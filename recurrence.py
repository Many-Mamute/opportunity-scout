"""Seasonal memory: most of these things run every year.

CERN opens applications in November. Formula Student registration opens in the
autumn. Summer schools post in February. The scout sees each of these exactly
once a year and then forgets, so every October it is starting from nothing.

This module records, per event family, the dates that were actually observed —
and next year, before the thing reappears, says "this opened around 12 November
last year, so expect it in about three weeks". That prediction is labelled as a
prediction everywhere it appears; it is never presented as a found fact.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from enrich import family_key
from models import Event

# A prediction is only worth making inside this window, and only from history
# recent enough to still describe the same programme.
LOOKAHEAD_DAYS = 45
MAX_HISTORY_AGE_DAYS = 800


def record(events: List[Event], state: dict, today: date) -> int:
    """Note this year's dates for anything with a usable date."""
    store = state.setdefault("recurrence", {})
    written = 0
    for ev in events:
        key = family_key(ev)
        if len(key) < 8:
            continue
        opens = next((s.opens for s in ev.stages if s.opens), None)
        closes = ev.next_deadline or next(
            (s.closes for s in ev.stages if s.closes), None)
        if not (opens or closes or ev.start_date):
            continue
        entry = {"year": today.year,
                 "title": ev.title[:80],
                 "opens": opens.isoformat() if opens else None,
                 "closes": closes.isoformat() if closes else None,
                 "start": ev.start_date.isoformat() if ev.start_date else None,
                 "seen": today.isoformat()}
        hist = store.setdefault(key, [])
        # One entry per family per year; later sightings refine the earlier one.
        for i, old in enumerate(hist):
            if old.get("year") == entry["year"]:
                hist[i] = {**old, **{k: v for k, v in entry.items() if v}}
                break
        else:
            hist.append(entry)
            written += 1
        del hist[:-4]                      # four years of history is plenty
    return written


def _shift_to(d: date, target_year: int) -> Optional[date]:
    try:
        return d.replace(year=target_year)
    except ValueError:                     # 29 February
        return d.replace(year=target_year, day=28)


def due_soon(state: dict, today: date,
             open_keys: set) -> List[Tuple[str, dict, date, int]]:
    """Families that opened around now in a previous year and are not open yet.

    Returns (family_key, history_entry, expected_date, days_away).
    """
    out = []
    for key, hist in (state.get("recurrence") or {}).items():
        if key in open_keys or not hist:
            continue
        last = hist[-1]
        anchor_s = last.get("opens") or last.get("closes") or last.get("start")
        if not anchor_s:
            continue
        anchor = date.fromisoformat(anchor_s)
        if (today - anchor).days > MAX_HISTORY_AGE_DAYS:
            continue
        expected = _shift_to(anchor, today.year)
        if expected and expected < today:
            expected = _shift_to(anchor, today.year + 1)
        if not expected:
            continue
        days = (expected - today).days
        if 0 <= days <= LOOKAHEAD_DAYS:
            out.append((key, last, expected, days))
    out.sort(key=lambda x: x[3])
    return out


def as_prompts(state: dict, today: date, open_keys: set) -> List[str]:
    """One human line per expected reopening, explicitly marked as a guess."""
    lines = []
    for key, last, expected, days in due_soon(state, today, open_keys):
        what = "opened" if last.get("opens") else \
               ("closed" if last.get("closes") else "ran")
        anchor = last.get("opens") or last.get("closes") or last.get("start")
        lines.append(
            f"{last.get('title', key)} — {what} on {anchor} last time, so "
            f"expect it around {expected} ({days} day(s) away). Not found open "
            f"yet; this is a prediction from last year, not a listing.")
    return lines
