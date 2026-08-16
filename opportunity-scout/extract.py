"""Extraction: JSON-LD first, LLM last.

schema.org Event structured data is exact and cannot hallucinate — always
preferred. Pages with no structured data are batched (~10 per request) into
Gemini Flash-Lite with a prompt that forbids inference. Missing facts stay
the literal string "not stated".
"""
from __future__ import annotations

import json
import re
import time
from datetime import date
from typing import List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from models import NOT_STATED, Event, Stage

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_URL = GEMINI_BASE + "/models/{model}:generateContent?key={key}"

# Google retires and renames Gemini models on its own schedule; a hardcoded
# name is a time bomb that silently empties the digest. On a 404 the scout asks
# the API which models actually exist and picks a Flash-Lite class one, then
# reuses that name for the rest of the run.
_MODEL_PREFERENCE = ["flash-lite", "flash", "pro"]


def discover_model(api_key: str, diagnostics: dict) -> Optional[str]:
    """Ask ListModels what this key can actually call."""
    try:
        r = requests.get(f"{GEMINI_BASE}/models?key={api_key}&pageSize=100",
                         timeout=20)
        r.raise_for_status()
        names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
    except (requests.RequestException, ValueError, KeyError) as e:
        diagnostics.setdefault("gemini_errors", []).append(
            f"ListModels failed ({type(e).__name__}) — the key itself is likely "
            f"invalid or the Generative Language API is not enabled for it")
        return None
    if not names:
        diagnostics.setdefault("gemini_errors", []).append(
            "ListModels returned no models supporting generateContent")
        return None
    for want in _MODEL_PREFERENCE:
        hits = sorted(n for n in names if want in n and "preview" not in n)
        if hits:
            return hits[-1]
    return names[0]

ANTI_FABRICATION = ("Extract only what is explicitly present in this text. "
                    "Use not stated for anything absent. Do not infer, "
                    "estimate, or complete partial information.")

LLM_FIELDS = ["title", "organiser", "one_line_summary", "location", "format",
              "start_date", "end_date", "deadline", "cost", "paid_unpaid",
              "team_requirement", "selectivity", "prerequisites", "age_limits",
              "rewards", "language"]


def _to_date(v) -> Optional[date]:
    if not v:
        return None
    try:
        return dateparser.isoparse(str(v)).date()
    except (ValueError, OverflowError):
        try:
            return dateparser.parse(str(v), dayfirst=True, fuzzy=False).date()
        except Exception:
            return None


def _clean(text: str, limit: int = 180) -> str:
    t = re.sub(r"<[^>]+>", " ", text or "")
    t = re.sub(r"\s+", " ", t).strip()
    return (t[: limit - 1] + "…") if len(t) > limit else t


# ------------------------------------------------------------------ JSON-LD
def _iter_nodes(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_nodes(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_nodes(v)


def parse_jsonld(html: str, page_url: str, source: str,
                 http_status: int) -> List[Event]:
    soup = BeautifulSoup(html or "", "html.parser")
    events: List[Event] = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text() or ""
        raw = re.sub(r"[\x00-\x08\x0b-\x1f]", " ", raw)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for node in _iter_nodes(data):
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if not any(isinstance(x, str) and x.endswith("Event") for x in types):
                continue
            ev = _map_event_node(node, page_url, source, http_status)
            if ev:
                events.append(ev)
    return events


def _map_event_node(node: dict, page_url: str, source: str,
                    status: int) -> Optional[Event]:
    name = node.get("name")
    if not name or not isinstance(name, str):
        return None
    ev = Event(title=_clean(name, 200), url=str(node.get("url") or page_url),
               source=source, http_status=status, confidence="verified")

    org = node.get("organizer") or node.get("organiser")
    if isinstance(org, list) and org:
        org = org[0]
    if isinstance(org, dict):
        ev.organiser = _clean(str(org.get("name", NOT_STATED)), 120)
    elif isinstance(org, str):
        ev.organiser = _clean(org, 120)

    ev.start_date = _to_date(node.get("startDate"))
    ev.end_date = _to_date(node.get("endDate")) or ev.start_date
    if ev.start_date and ev.end_date:
        ev.duration_days = (ev.end_date - ev.start_date).days + 1

    mode = str(node.get("eventAttendanceMode") or "")
    locs = node.get("location")
    locs = locs if isinstance(locs, list) else ([locs] if locs else [])
    parts, virtual = [], False
    for L in locs:
        if isinstance(L, str):
            parts.append(L)
            continue
        if not isinstance(L, dict):
            continue
        if "VirtualLocation" in str(L.get("@type", "")):
            virtual = True
            continue
        city = None
        addr = L.get("address")
        if isinstance(addr, dict):
            city = addr.get("addressLocality") or addr.get("addressRegion")
        elif isinstance(addr, str):
            city = addr
        piece = ", ".join(x for x in [L.get("name"), city] if x)
        if piece:
            parts.append(piece)
    if "Online" in mode or (virtual and not parts):
        ev.format = "online"
    elif "Mixed" in mode or (virtual and parts):
        ev.format = "hybrid"
    else:
        ev.format = "in_person"
    ev.location = _clean("; ".join(parts), 150) or \
        ("online" if ev.format == "online" else NOT_STATED)

    offers = node.get("offers")
    offers = offers if isinstance(offers, list) else ([offers] if offers else [])
    prices, currency, valid_through = [], "", None
    for o in offers:
        if not isinstance(o, dict):
            continue
        p = o.get("price")
        if p is None and isinstance(o.get("priceSpecification"), dict):
            p = o["priceSpecification"].get("price")
        try:
            if p is not None and str(p) != "":
                prices.append(float(str(p).replace(",", ".")))
        except ValueError:
            pass
        currency = o.get("priceCurrency") or currency
        vt = _to_date(o.get("validThrough"))
        if vt:
            valid_through = min(valid_through, vt) if valid_through else vt
    if prices:
        mx = max(prices)
        if mx == 0:
            ev.cost = "free"
        elif currency in ("EUR", "€", ""):
            ev.cost = f"paid (€{mx:g})"
        else:
            ev.cost = f"paid ({mx:g} {currency})"
    if valid_through:
        ev.stages.append(Stage(name="Registration closes", closes=valid_through))

    desc = node.get("description")
    if isinstance(desc, str):
        ev.one_line_summary = _clean(desc)
    return ev


# ------------------------------------------------------------- Gemini batch
class GeminiBudget:
    def __init__(self, cap: int):
        self.cap, self.used = cap, 0

    @property
    def remaining(self) -> int:
        return max(0, self.cap - self.used)


def llm_extract_batches(candidates: List[dict], api_key: str, model: str,
                        budget: GeminiBudget, batch_size: int = 10,
                        pause_s: float = 5.0,
                        diagnostics: Optional[dict] = None
                        ) -> Tuple[List[Event], List[dict]]:
    """candidates: [{url, source, http_status, text}]. Returns (events, leftovers).
    Leftovers are candidates dropped because the per-run LLM cap was reached —
    they are reported, never silently invented."""
    events: List[Event] = []
    leftovers: List[dict] = []
    diagnostics = diagnostics if diagnostics is not None else {}
    if not api_key:
        return events, candidates
    rediscovered = False
    for i in range(0, len(candidates), batch_size):
        batch = candidates[i:i + batch_size]
        if budget.remaining <= 0 or model is None:
            leftovers.extend(batch)
            continue
        budget.used += 1
        try:
            parsed = _one_gemini_call(batch, api_key, model)
        except Exception as e:                     # noqa: BLE001
            msg = str(e)[:200]
            if "404" in msg and not rediscovered:
                rediscovered = True
                found = discover_model(api_key, diagnostics)
                diagnostics.setdefault("gemini_errors", []).append(
                    f"model '{model}' returned 404; "
                    + (f"switched to '{found}'" if found
                       else "no usable model found"))
                model = found
                diagnostics["gemini_model_used"] = found
                if model:
                    try:
                        budget.used += 1
                        parsed = _one_gemini_call(batch, api_key, model)
                    except Exception as e2:        # noqa: BLE001
                        diagnostics["gemini_errors"].append(str(e2)[:200])
                        parsed = []
                else:
                    parsed = []
            else:
                diagnostics.setdefault("gemini_errors", []).append(msg)
                parsed = []
        for item in parsed:
            ev = _event_from_llm(item, batch)
            if ev:
                events.append(ev)
        if i + batch_size < len(candidates):
            time.sleep(pause_s)                    # free tier ≈15 RPM
    return events, leftovers


def _one_gemini_call(batch: List[dict], api_key: str, model: str) -> List[dict]:
    blocks = []
    for n, c in enumerate(batch, 1):
        text = re.sub(r"\s+", " ", c.get("text", ""))[:5000]
        blocks.append(f"### PAGE {n}\nURL: {c['url']}\nTEXT: {text}")
    prompt = (
        f"{ANTI_FABRICATION}\n\n"
        "For each numbered page below that describes ONE event, internship or "
        "programme, output one JSON object. Skip pages that are only listings "
        "or navigation. Respond with ONLY a JSON array, no prose, no markdown.\n"
        "Each object: {\"n\": <page number>, "
        + ", ".join(f'"{f}": <string, ISO date, or "not stated">' for f in LLM_FIELDS)
        + ", \"stages\": [{\"name\", \"opens\", \"closes\", \"is_advantageous\"}] "
        "listing only deadlines/rounds explicitly named in the text}.\n\n"
        + "\n\n".join(blocks))
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0,
                                 "response_mime_type": "application/json"}}
    r = requests.post(GEMINI_URL.format(model=model, key=api_key),
                      json=body, timeout=60)
    r.raise_for_status()
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
    out = json.loads(text)
    return out if isinstance(out, list) else []


def _event_from_llm(item: dict, batch: List[dict]) -> Optional[Event]:
    if not isinstance(item, dict):
        return None
    try:
        n = int(item.get("n", 0))
        src = batch[n - 1]
    except (ValueError, IndexError):
        return None
    title = item.get("title")
    if not title or title == NOT_STATED:
        return None

    def g(key, default=NOT_STATED):
        v = item.get(key)
        return v if isinstance(v, str) and v.strip() else default

    ev = Event(title=_clean(title, 200), url=src["url"], source=src["source"],
               http_status=src.get("http_status"), confidence="partial",
               organiser=g("organiser"), one_line_summary=_clean(g("one_line_summary", "")),
               location=g("location"), team_requirement=g("team_requirement"),
               selectivity=g("selectivity"), prerequisites=g("prerequisites"),
               age_limits=g("age_limits"), rewards=g("rewards", "none stated"),
               language=g("language"), cost=g("cost"))
    fmt = g("format", "").lower()
    if "online" in fmt:
        ev.format = "online"
    elif "hybrid" in fmt:
        ev.format = "hybrid"
    ev.start_date = _to_date(item.get("start_date")
                             if item.get("start_date") != NOT_STATED else None)
    ev.end_date = _to_date(item.get("end_date")
                           if item.get("end_date") != NOT_STATED else None) or ev.start_date
    if ev.start_date and ev.end_date:
        ev.duration_days = (ev.end_date - ev.start_date).days + 1
    pu = g("paid_unpaid", "")
    if pu and pu != NOT_STATED:
        ev.paid_unpaid = pu
    dl = _to_date(item.get("deadline") if item.get("deadline") != NOT_STATED else None)
    if dl:
        ev.stages.append(Stage(name="Application deadline", closes=dl))
    for st in item.get("stages") or []:
        if isinstance(st, dict) and st.get("name"):
            ev.stages.append(Stage(name=_clean(str(st["name"]), 80),
                                   opens=_to_date(st.get("opens")),
                                   closes=_to_date(st.get("closes")),
                                   is_advantageous=bool(st.get("is_advantageous"))))
    return ev


def snippet_event(title: str, url: str, snippet: str, source: str) -> Event:
    """Low-confidence record built only from a search result actually returned
    this run (e.g. LinkedIn/Facebook, which block direct fetching)."""
    return Event(title=_clean(title, 200), url=url, source=source,
                 one_line_summary=_clean(snippet), confidence="low")
