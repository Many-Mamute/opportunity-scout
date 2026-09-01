"""Hard filters. Every reject carries a reason string for the diagnostics.

Order of application (main.py): triage (pre-fetch) → classify/enrich →
geography → calendar → level → cost. Low-confidence snippet events skip
geography/calendar (facts unknown) and are quarantined in their own email
section instead.
"""
from __future__ import annotations

import re
from datetime import date, timedelta, timedelta
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
    _extend_calendar(cfg)


def _extend_calendar(cfg: dict) -> None:
    """Project the known academic year forward until the real one is published.

    The config holds FEUP 2026/27 only. Without this, a heavy event in January
    2028 sees no exam period at all and sails through unflagged — the calendar
    silently stops protecting you the moment the year ends. So each period is
    copied forward whole years and marked provisional; anything judged against
    a projected date says so on the card and in the diagnostics.
    """
    cal = cfg["calendar"]
    if cal.get("_extended"):
        return                                    # prime() is called repeatedly
    years = int(cal.get("project_years", 4))
    for key in ("exam_periods", "class_periods", "breaks"):
        base = [p for p in cal.get(key, []) if not p.get("provisional")]
        extra = []
        for n in range(1, years + 1):
            for p in base:
                try:
                    extra.append({"start": p["start"].replace(year=p["start"].year + n),
                                  "end": p["end"].replace(year=p["end"].year + n),
                                  "provisional": True})
                except (ValueError, AttributeError, KeyError):
                    continue                      # 29 Feb, or a malformed entry
        cal[key] = base + extra
    cal["_extended"] = True


def calendar_is_provisional(d: Optional[date], cfg: dict) -> bool:
    """True when the only periods covering this date are projected, not real."""
    if not d:
        return False
    for key in ("exam_periods", "class_periods", "breaks"):
        for p in cfg["calendar"].get(key, []):
            if _in_period(d, p):
                return bool(p.get("provisional"))
    return False


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
        # _online_hours always flags; whether a night start is fatal is your
        # call, expressed by settings.reject_night_online (default false).
        ok, reason = _online_hours(ev)
        if not ok and cfg["settings"].get("reject_night_online", False):
            return ok, reason
        return True, ""
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
    """Label the hour; never reject on it.

    An online session at 02:00 Lisbon may still be worth setting an alarm for,
    and that is a judgement call about one specific opportunity, not a rule.
    So the scout states the fact loudly and leaves the decision alone.
    (Set settings.reject_night_online: true to restore hard rejection.)
    """
    hour = None
    if ev.start_time and ev.tz_known:
        hour = int(ev.start_time[:2])
    else:
        m = re.search(r"\b([01]?\d|2[0-3]):[0-5]\d\b", ev.one_line_summary or "")
        if m:
            hour = int(m.group(1))
    if hour is None:
        return True, ""
    stamp = ev.start_time or f"{hour:02d}:00"
    if 0 <= hour < 7:
        ev.flags.append(f"starts {stamp} Lisbon — you would be up at night")
        return False, f"online: starts {stamp} Lisbon, in the middle of the night"
    if hour >= 22:
        ev.flags.append(f"starts {stamp} Lisbon — late night")
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
    if calendar_is_provisional(ev.start_date, cfg):
        ev.flags.append("term dates projected, not published")
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
    """Price hides an event only when it is genuinely out of range.

    Anything at or below never_exclude_below_eur is never filtered on price at
    all. Between that and max_eur the event is kept and the cost is stated
    plainly, with a note when nothing on the page justifies the fee — you can
    weigh that yourself. Only above max_eur, or an obvious certificate mill,
    is it dropped.
    """
    c = cfg["cost"]
    price = ev.price_eur()
    if price is None:
        return True, ""
    if price > c["max_eur"]:
        return False, f"cost: €{price:g} > €{c['max_eur']}"

    # A certificate mill is a quality judgement, not a price one, so this runs
    # before the cheap-enough shortcut — a €19 one is still worthless.
    hay = f"{ev.title} {ev.one_line_summary}".lower()
    if price > 0:
        milly = _any(c["certificate_mill_signals"], hay)
        live = _any(c["live_signals"], hay)
        named = ev.organiser != NOT_STATED
        if milly and not live and not named:
            return False, "cost: certificate mill (paid, self-paced, no cohort/institution)"

    if price <= c["never_exclude_below_eur"]:
        return True, ""

    basis = []
    if ev.rewards != "none stated":
        basis.append(f"rewards: {ev.rewards}")
    if ev.selectivity in ("competitive", "invite_only"):
        basis.append(f"{ev.selectivity} admission")
    if (ev.prestige_score or 0) >= 4:
        basis.append("recognised organiser")
    if basis:
        ev.value_justification = f"€{price:g} — what you get: " + "; ".join(basis)
    else:
        ev.value_justification = (
            f"€{price:g} and the page states no prize, certificate or "
            f"selection process — check what the fee actually buys")
        ev.flags.append("paid, with no stated benefit")

    return True, ""


# ----------------------------------------------------------------- pipeline
def relevance_filter(ev: Event, cfg: dict, today: date) -> Tuple[bool, str]:
    """The gate that stops a Porto events feed becoming a Porto nightlife feed.

    Three rules, cheapest first:
      1. social noise — pub crawls, speed dating, psychics: never relevant
         regardless of how the scoring shook out;
      2. retrospective — snippets where somebody is reminiscing about a past
         event rather than announcing an open one;
      3. no hook — a generic "other" with no field match and no deadline is
         not an opportunity, it is an activity.
    """
    hay = f"{ev.title} {ev.one_line_summary} {ev.organiser}"
    hit = _any(cfg.get("social_noise", []), hay)
    if hit:
        return False, f"social event, not an opportunity: matched '{hit}'"

    if ev.confidence == "low":
        hit = _any(cfg.get("retrospective", []), hay)
        if hit:
            return False, f"describes a past event: matched '{hit}'"

    if ev.type in ("other", "volunteer") and not ev.fields:
        return False, "no field match and no recognisable opportunity type"

    # An in-person event anywhere on earth used to reach the digest whenever it
    # arrived as a snippet, because snippets skip the geography filter (they
    # have no reliable location field to test). Hackathons in Nepal, Bangladesh
    # and Saudi Arabia all got through on the first run. So test the words
    # themselves: an in-person event must point at Portugal or clear the
    # funded/prestigious override.
    if ev.format != "online":
        hay_loc = f"{ev.location} {ev.title} {ev.one_line_summary} {ev.organiser}"
        geo = cfg["geography"]
        # Applies to everything: a verified event at "Porto Feliz" would
        # otherwise match the allowlist on the word "porto" and sail through.
        hit = _any(geo.get("false_porto", []), hay_loc)
        if hit:
            return False, f"a different Porto — matched '{hit}'"
        # Snippets only. Verified events have a real location field and get the
        # geocoder-backed geography_filter instead; testing both would reject
        # legitimate events whose page simply does not restate the country.
        if ev.confidence == "low" and not is_override(ev, cfg):
            markers = (list(geo["allowlist"].keys())
                       + list(geo.get("portugal_extra_markers", [])))
            if not _any(markers, hay_loc):
                return False, "no stated connection to Portugal"

    if ev.start_date and ev.start_date < today:
        return False, "already started"
    if ev.confidence == "low" and not ev.fields:
        return False, "snippet with no field match — not worth your attention"
    return True, ""


def apply_all(ev: Event, cfg: dict, geocoder=None,
              today: Optional[date] = None) -> Tuple[bool, str]:
    today = today or date.today()

    def _eligibility(e, c):
        return eligibility_filter(e, c, today)

    ok, reason = relevance_filter(ev, cfg, today)
    if not ok:
        return False, reason
    for fn in (geography_filter, calendar_filter, level_filter,
               cost_filter, _eligibility):
        ok, reason = fn(ev, cfg) if fn is not geography_filter \
            else geography_filter(ev, cfg, geocoder)
        if not ok:
            return False, reason
    return True, ""


# ------------------------------------------------- academic progress gating
_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "um": 1, "uma": 1, "dois": 2, "duas": 2, "três": 3, "tres": 3,
    "quatro": 4, "cinco": 5, "seis": 6, "sete": 7, "oito": 8, "nove": 9,
    "dez": 10, "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "primeiro": 1, "segundo": 2, "terceiro": 3, "quarto": 4, "quinto": 5,
}


def _num(tok: str) -> Optional[int]:
    tok = (tok or "").strip().lower()
    if tok.isdigit():
        return int(tok)
    # Look the whole word up BEFORE stripping ordinal suffixes: "third" ends
    # in "rd" and became "thi", which matched nothing.
    if tok in _WORD_NUMBERS:
        return _WORD_NUMBERS[tok]
    stripped = re.sub(r"(st|nd|rd|th|º|ª)$", "", tok)
    if stripped.isdigit():
        return int(stripped)
    return _WORD_NUMBERS.get(stripped)


def completed_semesters(on: date, cfg: dict) -> int:
    """How many semesters you will have finished by a given date."""
    a = cfg["level"]["academic"]
    s1, s2 = a["first_semester_end"], a["second_semester_end"]
    if on < s1:
        return 0
    done = 0
    year = 0
    while year < 12:
        e1 = s1.replace(year=s1.year + year)
        e2 = s2.replace(year=s2.year + year)
        if on >= e1:
            done += 1
        if on >= e2:
            done += 1
        if on < e2:
            break
        year += 1
    return min(done, a["degree_semesters"])


def required_semesters(text: str, cfg: dict) -> Tuple[Optional[int], str]:
    """Read an eligibility bar out of prose. Returns (semesters, the phrase)."""
    low = re.sub(r"\s+", " ", (text or "").lower())
    total = cfg["level"]["academic"]["degree_semesters"]

    # "at least six semesters", "completed 4 semesters", "seis semestres"
    m = re.search(r"(?:at least|minimum of|completed|conclu[íi]d[oa]s?|pelo menos)"
                  r"[^.]{0,30}?\b([a-zçãéêí]+|\d+)\s+(?:full[- ]time\s+)?"
                  r"(semesters?|semestres?)\b", low)
    if m and _num(m.group(1)):
        return _num(m.group(1)), m.group(0)[:80]
    m = re.search(r"\b([a-zçãéêí]+|\d+)\s+(?:full[- ]time\s+)?(?:semesters?|semestres?)"
                  r"\s+(?:of|de)\s+(?:university|higher|full[- ]time|ensino)", low)
    if m and _num(m.group(1)):
        return _num(m.group(1)), m.group(0)[:80]

    # "at least two years of university study"
    m = re.search(r"(?:at least|minimum of|completed|pelo menos|conclu[íi]dos?)"
                  r"[^.]{0,30}?\b([a-zçãéêí]+|\d+)\s+(?:academic\s+|full[- ]time\s+)?"
                  r"(?:years?|anos?)\s+(?:of|de)\s+"
                  r"(?:university|higher|undergraduate|study|studies|ensino|curso)", low)
    if m and _num(m.group(1)):
        return _num(m.group(1)) * 2, m.group(0)[:80]

    # "final year", "penultimate year" — relative to the whole degree
    if re.search(r"\b(final|last|graduating)[- ]year\b|\b[úu]ltimo ano\b", low):
        return total - 2, "final year"
    if re.search(r"\bpenultimate[- ]year\b|\bpen[úu]ltimo ano\b", low):
        return total - 4, "penultimate year"

    # "third-year students", "3rd year or above", "terceiro ano"
    m = re.search(r"\b([a-zçãéêí]+|\d+)(?:st|nd|rd|th|º)?[- ]?(?:year|ano)\b"
                  r"(?:\s+(?:students?|undergraduates?|estudantes?|or above|"
                  r"e acima|ou superior))?", low)
    if m and _num(m.group(1)) and re.search(
            r"(students?|undergraduates?|estudantes?|or above|e acima|ou superior|"
            r"enrolled|inscritos)", low):
        n = _num(m.group(1))
        if 2 <= n <= total // 2:            # "1st year" is no barrier
            return (n - 1) * 2, m.group(0)[:80]
    return None, ""


def eligible_from(need: int, cfg: dict) -> Optional[date]:
    """The first date on which you would have `need` semesters behind you.

    Knowing this turns a rejection into an appointment: CERN is not "no", it is
    "not until 2030", which is worth recording rather than throwing away.
    """
    a = cfg["level"]["academic"]
    if need > a["degree_semesters"]:
        return None
    s1, s2 = a["first_semester_end"], a["second_semester_end"]
    done = 0
    for year in range(0, 12):
        for end in (s1.replace(year=s1.year + year), s2.replace(year=s2.year + year)):
            done += 1
            if done >= need:
                return end
    return None


def eligibility_filter(ev: Event, cfg: dict,
                       today: Optional[date] = None) -> Tuple[bool, str]:
    """Reject anything that needs more of a degree than you will have done.

    The CERN Summer Student Programme states six completed semesters. That
    sentence was already being extracted and printed on the card — it simply
    was never compared against where you are, so the scout recommended
    something you could not apply to.
    """
    today = today or date.today()
    a = cfg["level"]["academic"]
    hay = f"{ev.prerequisites} {ev.one_line_summary} {ev.age_limits} {ev.title}"
    need, phrase = required_semesters(hay, cfg)
    if not need:
        return True, ""
    when = ev.start_date or ev.next_deadline or (
        today + timedelta(days=int(a["assume_within_days"])))
    have = completed_semesters(when, cfg)
    if need > have:
        ev.eligible_from = eligible_from(need, cfg)
        ev.flags.append(f"needs {need} semesters, you will have {have}")
        when_ready = (f"; you would qualify from {ev.eligible_from}"
                      if ev.eligible_from else "")
        return False, (f"eligibility: requires {need} completed semesters "
                       f"(\"{phrase.strip()}\"); by {when} you will have "
                       f"{have}{when_ready}")
    return True, ""
