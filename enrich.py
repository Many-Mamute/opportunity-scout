"""Second-opinion pass: fill gaps from sources independent of the original.

The listing page is often thin — no dates, no cost, an organiser the scout has
never heard of. Rather than shrugging with "not stated" and a default score of
2, this pass runs one search per gap-ridden event and reads what *other* people
say about it.

"Independent" is enforced, not assumed. A result is discarded when it:
  - shares a registrable domain with the event's own URL, or with another
    already-accepted corroborator (two pages on the same site are one source);
  - reads as a copy of the original description (high similarity), which is
    what aggregators and syndicated press releases are; or
  - is the organiser's own social profile.

Anything that survives is quoted to the model under the same anti-fabrication
instruction used everywhere else, and every filled field records which domains
supported it. A fact with no independent support stays "not stated".
"""
from __future__ import annotations

import difflib
import re
from datetime import date
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from extract import ANTI_FABRICATION, _one_gemini_call_raw, discover_model
from models import NOT_STATED, Event

# Sites that republish rather than report. A hit here is not a second opinion.
AGGREGATORS = (
    "eventbrite.", "lu.ma", "meetup.com", "facebook.com", "instagram.com",
    "linkedin.com", "x.com", "twitter.com", "eventful.", "allevents.in",
    "10times.com", "eventmapp", "viagogo", "ticketmaster", "youtube.com",
    "pinterest.", "tiktok.com",
)
SIMILARITY_LIMIT = 0.72       # above this, the "source" is quoting the original


def registrable(url: str) -> str:
    """Good-enough eTLD+1: 'careers.bosch.pt' and 'bosch.pt' are one source."""
    host = urlparse(url or "").netloc.lower().split(":")[0]
    host = host[4:] if host.startswith("www.") else host
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    # Handle the common two-part suffixes we actually meet (co.uk, com.br, ...).
    if parts[-2] in ("co", "com", "org", "gov", "ac", "edu") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _too_similar(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return difflib.SequenceMatcher(
        None, a.lower()[:400], b.lower()[:400]).ratio() > SIMILARITY_LIMIT


def independent_results(ev: Event, results: List[dict]) -> List[dict]:
    """Keep only genuinely separate voices, one per domain."""
    own = registrable(ev.url)
    seen_domains = {own} if own else set()
    kept = []
    for r in results:
        url, text = r.get("url", ""), r.get("content", "") or ""
        dom = registrable(url)
        if not dom or dom in seen_domains:
            continue
        if any(a in dom for a in AGGREGATORS):
            continue
        if _too_similar(text, ev.one_line_summary):
            continue
        seen_domains.add(dom)
        kept.append({"domain": dom, "url": url, "title": r.get("title", ""),
                     "text": text[:900]})
    return kept


def needs_enrichment(ev: Event, cfg: dict) -> List[str]:
    """Which gaps are worth spending a search on."""
    gaps = []
    if not ev.start_date:
        gaps.append("dates")
    if ev.cost == NOT_STATED:
        gaps.append("cost")
    default = cfg["estimates"]["base_prestige_default"]
    if (ev.prestige_score or 0) <= default and ev.organiser != NOT_STATED:
        gaps.append("organiser standing")
    if ev.next_deadline is None and not ev.stages:
        gaps.append("deadline")
    return gaps


def _query(ev: Event) -> str:
    title = re.sub(r"\s+", " ", ev.title).strip()[:90]
    org = "" if ev.organiser == NOT_STATED else f" {ev.organiser}"
    return f'"{title}"{org}'.strip()


PROMPT = """You are given an event and several SHORT EXCERPTS from independent
websites that mention it. """ + ANTI_FABRICATION + """

Use ONLY the excerpts. For each field, also name which excerpt number supports
it. If no excerpt states a field, return "not stated" for it and cite nothing.
Never merge two different events: if the excerpts describe something else, set
"about_this_event": false.

Return JSON only:
{"about_this_event": true|false,
 "start_date": "YYYY-MM-DD or not stated", "end_date": "YYYY-MM-DD or not stated",
 "application_deadline": "YYYY-MM-DD or not stated",
 "cost": "free | funded | EUR amount | not stated",
 "location": "city, country or not stated",
 "organiser_description": "one sentence on who the organiser is, or not stated",
 "reputation": "one sentence on how participants or press describe it, or not stated",
 "reputation_level": "international | national | regional | local | unknown",
 "supported_by": [excerpt numbers used]}
"""


def _build_prompt(ev: Event, sources: List[dict]) -> str:
    blocks = [f"EVENT TITLE: {ev.title}\nORGANISER: {ev.organiser}\n"]
    for i, s in enumerate(sources, 1):
        blocks.append(f"--- EXCERPT {i} (from {s['domain']}) ---\n"
                      f"{s['title']}\n{s['text']}")
    return PROMPT + "\n\n" + "\n\n".join(blocks)


def _as_date(v) -> Optional[date]:
    if not v or not isinstance(v, str) or v.strip().lower() in ("not stated", ""):
        return None
    try:
        return date.fromisoformat(v.strip()[:10])
    except ValueError:
        return None


def apply_findings(ev: Event, data: dict, sources: List[dict],
                   cfg: dict) -> List[str]:
    """Write only genuinely new facts, and record who supported each one."""
    if not data.get("about_this_event", False):
        return []
    doms = ", ".join(s["domain"] for s in sources)
    filled = []

    if not ev.start_date:
        sd = _as_date(data.get("start_date"))
        if sd:
            ev.start_date = sd
            ev.end_date = _as_date(data.get("end_date")) or sd
            if ev.end_date and ev.start_date:
                ev.duration_days = (ev.end_date - ev.start_date).days + 1
            filled.append("dates")
    dl = _as_date(data.get("application_deadline"))
    stage_name = "Application deadline (from other sources)"
    if dl and not ev.next_deadline and \
            not any(s.name == stage_name for s in ev.stages):
        from models import Stage
        ev.stages.append(Stage(name=stage_name, closes=dl))
        filled.append("deadline")
    if ev.cost == NOT_STATED:
        c = (data.get("cost") or "").strip()
        if c and c.lower() != "not stated":
            low = c.lower()
            ev.cost = ("free" if "free" in low or low in ("0", "€0")
                       else "funded" if "fund" in low
                       else f"paid ({c})")
            filled.append("cost")
    if ev.location == NOT_STATED and ev.format != "online":
        loc = (data.get("location") or "").strip()
        if loc and loc.lower() != "not stated":
            ev.location = loc
            filled.append("location")

    # Prestige from what others say, instead of a default for "unrecognised".
    level = (data.get("reputation_level") or "unknown").lower()
    rep = (data.get("reputation") or "").strip()
    desc = (data.get("organiser_description") or "").strip()
    mapped = {"international": 4, "national": 3, "regional": 2, "local": 2}.get(level)
    if mapped and mapped > (ev.prestige_score or 0):
        ev.prestige_score = mapped
        ev.estimate_notes["prestige"] = (
            f"Prestige {mapped} out of 5, not from the name-matching table but "
            f"from independent coverage: {doms} describe this as {level} in "
            f"reach." + (f" {rep}" if rep and rep.lower() != "not stated" else ""))
        filled.append("prestige")
    if desc and desc.lower() != "not stated" and ev.organiser_note is None:
        ev.organiser_note = desc

    if filled:
        ev.corroborated_by = sorted({s["domain"] for s in sources})
        ev.corroboration_note = (
            f"{', '.join(filled)} confirmed against {len(sources)} independent "
            f"source(s): {doms}")
        ev.flags.append("checked against other sources")
    return filled


def enrich(events: List[Event], cfg: dict, api_key_tavily: str,
           gemini_key: str, model: Optional[str], tavily_budget,
           gemini_budget, diagnostics: dict,
           out_of_time=lambda: False) -> None:
    """One search per event, most-promising first, until a budget runs out."""
    settings = cfg["settings"]
    max_events = int(settings.get("enrichment_max_events", 6))
    if not api_key_tavily or not gemini_key or not model:
        diagnostics["enrichment"] = "skipped — missing API key or model"
        return
    done, filled_total, no_support = 0, 0, 0
    rediscovered = False
    for ev in events:
        if done >= max_events or out_of_time():
            break
        gaps = needs_enrichment(ev, cfg)
        if not gaps:
            continue
        # Some events simply have no other coverage. Re-searching them every
        # morning burns a credit a day for the same empty answer.
        retry_days = int(settings.get("enrichment_retry_days", 14))
        if ev.enrichment_attempted and \
                (date.today() - ev.enrichment_attempted).days < retry_days:
            continue
        ev.enrichment_attempted = date.today()
        if not tavily_budget.take():
            diagnostics["enrichment_stopped"] = "Tavily budget exhausted"
            break
        done += 1
        try:
            r = requests.post("https://api.tavily.com/search",
                              json={"api_key": api_key_tavily, "query": _query(ev),
                                    "search_depth": "basic", "max_results": 6},
                              timeout=25)
            r.raise_for_status()
            results = r.json().get("results", [])
        except (requests.RequestException, ValueError) as e:
            diagnostics.setdefault("enrichment_errors", []).append(
                f"{ev.id or ev.title[:30]}: {type(e).__name__}")
            continue
        sources = independent_results(ev, results)[:4]
        if not sources:
            no_support += 1
            ev.corroboration_note = ("searched, but every result came from the "
                                     "same site or simply repeated it")
            continue
        if gemini_budget.remaining <= 0:
            break
        gemini_budget.used += 1
        try:
            data = _one_gemini_call_raw(_build_prompt(ev, sources),
                                        gemini_key, model)
        except Exception as e:                      # noqa: BLE001
            msg = str(e)[:160]
            if "404" in msg and not rediscovered:
                rediscovered = True
                model = discover_model(gemini_key, diagnostics)
                diagnostics["gemini_model_used"] = model
                if not model:
                    break
                try:
                    gemini_budget.used += 1
                    data = _one_gemini_call_raw(_build_prompt(ev, sources),
                                                gemini_key, model)
                except Exception as e2:             # noqa: BLE001
                    diagnostics.setdefault("enrichment_errors", []).append(str(e2)[:120])
                    continue
            else:
                diagnostics.setdefault("enrichment_errors", []).append(msg[:120])
                continue
        if isinstance(data, dict):
            filled_total += len(apply_findings(ev, data, sources, cfg))
    diagnostics["enrichment"] = (
        f"{done} event(s) searched, {filled_total} field(s) filled from "
        f"independent sources"
        + (f", {no_support} found no independent coverage" if no_support else ""))


# --------------------------------------------------------- domestic twins
_COUNTRY_WORDS = re.compile(
    r"\b(portugal|portuguese|espanha|spain|germany|deutschland|alemanha|france|"
    r"fran[çc]a|italy|it[áa]lia|netherlands|holanda|belgium|b[ée]lgica|uk|"
    r"united kingdom|austria|[áa]ustria|switzerland|su[íi][çc]a|czech|hungary|"
    r"poland|pol[óo]nia|sweden|su[ée]cia|norway|denmark|finland|greece|"
    r"gr[ée]cia|ireland|irlanda|east|west|north|south)\b", re.I)
_YEARS = re.compile(r"\b(20\d{2})(/\d{2,4})?\b")


def family_key(ev: Event) -> str:
    """What remains of a title once the country and year are stripped.

    'Formula Student Portugal 2027' and 'Formula Student Germany 2027' collapse
    to the same key, which is how the scout notices you are being offered the
    German edition of something that also runs here.
    """
    t = _YEARS.sub(" ", ev.title or "")
    t = _COUNTRY_WORDS.sub(" ", t)
    t = re.sub(r"[^\w\s]", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def _is_domestic(ev: Event, cfg: dict) -> bool:
    hay = f"{ev.location} {ev.title} {ev.organiser}".lower()
    markers = (list(cfg["geography"]["allowlist"].keys())
               + list(cfg["geography"].get("portugal_extra_markers", [])))
    return any(re.search(m, hay) for m in markers)


def mark_domestic_twins(events: List[Event], cfg: dict) -> int:
    """Flag a foreign event when a Portuguese edition of the same thing is open.

    Not a rejection — the German round of a competition can be the better one.
    But you should be told, rather than working it out from two cards that look
    unrelated until you read the titles carefully.
    """
    families: Dict[str, List[Event]] = {}
    for ev in events:
        key = family_key(ev)
        if len(key) >= 8:                       # ignore titles that erode to noise
            families.setdefault(key, []).append(ev)
    marked = 0
    for key, group in families.items():
        if len(group) < 2:
            continue
        domestic = [e for e in group if _is_domestic(e, cfg)]
        foreign = [e for e in group if e not in domestic]
        if not domestic or not foreign:
            continue
        home = domestic[0]
        for ev in foreign:
            ev.domestic_twin = home.id or home.title[:40]
            ev.flags.append(f"a Portuguese edition exists: {ev.domestic_twin}")
            marked += 1
    return marked
