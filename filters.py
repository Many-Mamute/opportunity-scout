"""Hard filters. Every reject carries a reason string for the diagnostics.

Order of application (main.py): triage (pre-fetch) → classify/enrich →
geography → calendar → level → cost. Low-confidence snippet events skip
geography/calendar (facts unknown) and are quarantined in their own email
section instead.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from models import NOT_STATED, Event
from store import normalise


def _any(patterns: List[str], text: str) -> Optional[str]:
    for p in patterns or []:
        if re.search(p, text, re.IGNORECASE):
            return p
    return None


# ------------------------------------------------------------- stage 1 triage
def triage(title: str, url: str, snippet: str, cfg: dict) -> bool:
    """Keyword/regex pass on title+url+snippet before any fetch. Zero cost."""
    hay = f"{title} {url} {snippet}"
    if _any(cfg["triage"]["blacklist"], hay):
        return False
    return _any(cfg["triage"]["include_any"], hay) is not None


# ------------------------------------------------------------- classification
def classify(ev: Event, cfg: dict, page_text: str = "") -> None:
    hay = f"{ev.title} {ev.one_line_summary} {ev.url} {page_text[:3000]}"
    if ev.type == "other":
        for t, pats in cfg["classify"]["type"].items():
            if _any(pats, hay):
                ev.type = t
                break
    fields = [f for f, pats in cfg["classify"]["fields"].items() if _any(pats, hay)]
    ev.fields = fields or ev.fields
    if ev.language == NOT_STATED:
        pt = len(re.findall(r"\b(que|para|com|uma|dos|das|não|são)\b", hay.lower()))
        en = len(re.findall(r"\b(the|and|with|for|will|your)\b", hay.lower()))
        if pt and en:
            ev.language = "Portuguese & English"
        elif pt > 1:
            ev.language = "Portuguese"
        elif en > 1:
            ev.language = "English"
    _fact_regexes(ev, hay)


def _fact_regexes(ev: Event, hay: str) -> None:
    """Conservative captures of explicit on-page statements only."""
    low = hay.lower()
    if ev.team_requirement == NOT_STATED:
        if re.search(r"teams? of \d|equipas? de \d|team.based", low):
            ev.team_requirement = "team_required"
        elif re.search(r"join (an? )?(existing )?team|individual(ly)? or (in )?teams?", low):
            ev.team_requirement = "can_join_existing_team"
        elif re.search(r"\b(individual|solo)\b", low):
            ev.team_requirement = "solo"
    if ev.selectivity == NOT_STATED:
        if re.search(r"invite.only", low):
            ev.selectivity = "invite_only"
        elif re.search(r"selection|selective|competitive admission|limited (places|spots)", low):
            ev.selectivity = "competitive"
        elif re.search(r"application|apply|candidatura", low):
            ev.selectivity = "application"
        elif re.search(r"open to (all|everyone)|free entry|registration open", low):
            ev.selectivity = "open"
    if ev.rewards == "none stated":
        found = []
        m = re.search(r"[€$]\s?([\d.,]+)k?\s*(?:in )?priz|prize (pool|money)[^\d]*[€$]?\s?([\d.,]+)", low)
        if m:
            found.append("prize_money (stated)")
        for pat, label in [(r"\bects\b", "ECTS"), (r"certificate|certificado", "certificate"),
                           (r"diploma", "diploma"), (r"reference letter|carta de recomenda", "reference_letter"),
                           (r"interview|fast.track", "interview_pipeline"),
                           (r"published|publica[çc][ãa]o", "published_work")]:
            if re.search(pat, low):
                found.append(label)
        if found:
            ev.rewards = ", ".join(dict.fromkeys(found))
    if ev.age_limits == NOT_STATED:
        m = re.search(r"(1[6-9]|2[01])\s?\+|\bunder\s?(19|21)\b|idade m[íi]nima\s?(\d{2})", low)
        if m:
            ev.age_limits = m.group(0)
    if ev.cost == NOT_STATED:
        if re.search(r"\bfree( of charge)?\b|gratuit[oa]|entrada livre", low):
            ev.cost = "free"
        elif _any_funded(low):
            ev.cost = "funded"
    if ev.type == "internship" and not ev.paid_unpaid:
        m = re.search(r"(paid internship|est[áa]gio remunerado|remunerad[oa])", low)
        if m:
            ev.paid_unpaid = "paid (amount not stated)"
        elif re.search(r"unpaid|n[ãa]o remunerad", low):
            ev.paid_unpaid = "unpaid"
        else:
            ev.paid_unpaid = NOT_STATED


def _any_funded(low: str) -> bool:
    from_cfg = _FUNDED_CACHE.get("pats", [])
    return _any(from_cfg, low) is not None


_FUNDED_CACHE: Dict[str, List[str]] = {}


def prime(cfg: dict) -> None:
    _FUNDED_CACHE["pats"] = cfg["override"]["funded_keywords"]


# ---------------------------------------------------------------- geography
def resolve_municipality(location: str, cfg: dict,
                         geocoder=None) -> Tuple[Optional[str], Optional[int]]:
    """Hardcoded allowlist first; Nominatim (via injected geocoder) only for
    unrecognised names; every resolution cached permanently by the geocoder."""
    loc_norm = normalise(location or "")
    allow = cfg["geography"]["allowlist"]
    for muni, minutes in allow.items():
        if muni in loc_norm:
            return muni, int(minutes)
    if geocoder and location and location != NOT_STATED:
        muni = geocoder(location)
        if muni and normalise(muni) in allow:
            m = normalise(muni)
            return m, int(allow[m])
        return (normalise(muni) if muni else None), None
    return None, None


def is_override(ev: Event, cfg: dict) -> bool:
    """Funded-or-prestigious: admitted regardless of location."""
    if ev.cost == "funded" or ev.cost.startswith("stipend"):
        return True
    org = normalise(ev.organiser)
    hay = normalise(f"{ev.organiser} {ev.title}")
    return any(p in org or p in hay for p in
               (normalise(x) for x in cfg["override"]["prestigious_organisers"]))


def geography_filter(ev: Event, cfg: dict, geocoder=None) -> Tuple[bool, str]:
    if ev.format == "online":
        return _online_hours(ev)
    override = is_override(ev, cfg)
    muni, minutes = resolve_municipality(ev.location, cfg, geocoder)
    if minutes is not None:
        ev.travel_time_from_porto_min = minutes
    if override:
        if muni not in cfg["geography"]["allowlist"] or \
                (ev.is_multi_day() and muni not in cfg["geography"]["multi_day_municipalities"]):
            ev.flags.append("worth the travel")
        return True, ""
    if muni is None:
        return False, "geography: location unverifiable"
    if ev.is_multi_day():
        if muni in cfg["geography"]["multi_day_municipalities"]:
            return True, ""
        return False, f"geography: multi-day outside Porto ({muni})"
    if muni in cfg["geography"]["allowlist"]:
        return True, ""
    return False, f"geography: {muni} outside 1h allowlist"


def _online_hours(ev: Event) -> Tuple[bool, str]:
    """Reject only if core sessions are stated to fall 00:00–07:00 Lisbon;
    flag if merely awkward; unknown times pass (never infer)."""
    m = re.search(r"\b([01]?\d|2[0-3]):[0-5]\d\b", ev.one_line_summary or "")
    if not m:
        return True, ""
    hour = int(m.group(1))
    if 0 <= hour < 7:
        return False, "online: core sessions 00:00–07:00 Lisbon"
    if hour >= 22:
        ev.flags.append("awkward hours")
    return True, ""


# ------------------------------------------------------------------ calendar
def _in_period(d: date, period: dict) -> bool:
    return period["start"] <= d <= period["end"]


def next_exam_start(after: date, cfg: dict) -> Optional[date]:
    starts = sorted(p["start"] for p in cfg["calendar"]["exam_periods"])
    for s in starts:
        if s >= after:
            return s
    return None


def calendar_filter(ev: Event, cfg: dict) -> Tuple[bool, str]:
    cal = cfg["calendar"]
    hours = ev.estimated_effort_hours or 0
    start, end = ev.start_date, ev.end_date or ev.start_date
    if not start:
        return True, ""                       # dates not stated → cannot judge
    overlaps_exams = any(
        _in_period(start, p) or _in_period(end, p) or (start <= p["start"] <= end)
        for p in cal["exam_periods"])
    if overlaps_exams:
        if hours < cal["light_hours"]:
            ev.calendar_conflict = "near_exams"
            ev.flags.append("during exams — light only")
            return True, ""
        ev.calendar_conflict = "blocked"
        return False, "calendar: non-light event during exam period"
    if hours >= cal["heavy_hours"]:
        nxt = next_exam_start(end, cfg)
        if nxt and (nxt - end).days < cal["heavy_buffer_days"]:
            ev.calendar_conflict = "blocked"
            return False, (f"calendar: heavy event ends {end}, "
                           f"<{cal['heavy_buffer_days']}d before exams {nxt}")
    in_class = any(_in_period(start, p) or _in_period(end, p)
                   for p in cal["class_periods"])
    in_break = any(_in_period(start, b) and _in_period(end, b)
                   for b in cal["breaks"])
    if in_class and not in_break and hours < cal["heavy_hours"]:
        ev.calendar_conflict = "clashes_with_term_time"
        ev.flags.append("clashes with term time")
    elif hours >= cal["heavy_hours"]:
        nxt = next_exam_start(end, cfg)
        if nxt and (nxt - end).days < cal["heavy_buffer_days"] + 14:
            ev.calendar_conflict = "near_exams"
    return True, ""


# --------------------------------------------------------------------- level
def level_filter(ev: Event, cfg: dict) -> Tuple[bool, str]:
    hay = f"{ev.title} {ev.one_line_summary} {ev.prerequisites}"
    hit = _any(cfg["level"]["reject_patterns"], hay)
    if hit:
        return False, f"level: matches '{hit}'"
    if _any(cfg["level"]["advantage_age_patterns"], hay):
        ev.flags.append("age-category advantage")
    m = re.search(r"(\d{2})\s?\+|\bidade m[íi]nima:?\s?(\d{2})", hay.lower())
    if m:
        min_age = int(m.group(1) or m.group(2))
        if min_age > cfg["level"]["min_age_user"]:
            return False, f"level: minimum age {min_age}"
    return True, ""


# ---------------------------------------------------------------------- cost
def cost_filter(ev: Event, cfg: dict) -> Tuple[bool, str]:
    c = cfg["cost"]
    price = ev.price_eur()
    if price is not None and price > c["max_eur"]:
        return False, f"cost: €{price:g} > €{c['max_eur']}"
    if price is not None and price > c["justification_above_eur"]:
        basis = []
        if ev.rewards != "none stated":
            basis.append(f"rewards: {ev.rewards}")
        if ev.selectivity in ("competitive", "invite_only"):
            basis.append(f"{ev.selectivity} admission")
        if (ev.prestige_score or 0) >= 4:
            basis.append("high-prestige organiser")
        if not basis:
            return False, f"cost: €{price:g} with no stated value basis"
        ev.value_justification = (f"€{price:g} earns it: " + "; ".join(basis))
    hay = f"{ev.title} {ev.one_line_summary}".lower()
    if price is not None and price > 0:
        milly = _any(c["certificate_mill_signals"], hay)
        live = _any(c["live_signals"], hay)
        named = ev.organiser != NOT_STATED
        if milly and not live and not named:
            return False, "cost: certificate mill (paid, self-paced, no cohort/institution)"
    return True, ""


# ----------------------------------------------------------------- pipeline
def apply_all(ev: Event, cfg: dict, geocoder=None) -> Tuple[bool, str]:
    for fn in (geography_filter, calendar_filter, level_filter, cost_filter):
        ok, reason = fn(ev, cfg) if fn is not geography_filter \
            else geography_filter(ev, cfg, geocoder)
        if not ok:
            return False, reason
    return True, ""
