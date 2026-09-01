"""Typed schema for the Opportunity Scout. Pydantic v2.

Anti-fabrication contract: every factual field defaults to the literal
string "not stated" (or None for dates/numbers). Only estimated_effort_hours,
cv_value_score, prestige_score and networking_score may be estimates, and
each estimate carries a reason in `estimate_notes` so the email can label it.
"""
from __future__ import annotations

from datetime import date
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

NOT_STATED = "not stated"

# Flags are recomputed from scratch on every run by the filters and scorer.
# Only these survive a reload — they record something you told the scout, not
# something it worked out. Everything else is cleared before re-deriving, or
# the same flag is appended again every morning and seen.jsonl grows forever.
STICKY_FLAGS = frozenset({"you marked this interesting"})
BUILD = "2026-08-31.4"   # shown in Diagnostics; bump when you change the code

EventType = Literal[
    "internship", "competition", "hackathon", "masterclass",
    "summer_school", "workshop", "conference", "volunteer", "other",
]
FieldTag = Literal[
    "mechanical", "industrial_ops", "automotive_motorsport", "business",
    "finance_quant", "data_ai", "entrepreneurship",
    "public_speaking", "economics_literacy",
]
FormatT = Literal["in_person", "online", "hybrid"]
Confidence = Literal["verified", "partial", "low"]
Intensity = Literal["light", "moderate", "heavy", "intense"]
CalendarConflict = Literal["none", "clashes_with_term_time", "near_exams", "blocked"]


class Stage(BaseModel):
    """One round of a multi-stage application (early-bird, qualifier, ...)."""
    name: str
    opens: Optional[date] = None
    closes: Optional[date] = None
    notes: str = ""
    is_advantageous: bool = False
    # which alerts have already been emailed, e.g. ["opens", "t7"]
    alerts_sent: List[str] = Field(default_factory=list)


class Event(BaseModel):
    id: str = ""                      # E-0247, assigned by the store
    title: str
    organiser: str = NOT_STATED
    one_line_summary: str = ""
    type: EventType = "other"
    fields: List[FieldTag] = Field(default_factory=list)
    format: FormatT = "in_person"
    location: str = NOT_STATED
    travel_time_from_porto_min: Optional[int] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    # Clock times, already converted to Europe/Lisbon so the digest never makes
    # you do timezone arithmetic. tz_known=False means the source published a
    # bare time with no offset, so it is shown as published and labelled.
    start_time: Optional[str] = None      # "HH:MM", Lisbon
    end_time: Optional[str] = None
    tz_known: bool = False
    tz_source: Optional[str] = None       # e.g. "UTC-06:00", for the label
    # Second-opinion pass (enrich.py): which independent domains backed which
    # facts, and what they said about an organiser the tables did not know.
    corroborated_by: List[str] = Field(default_factory=list)
    corroboration_note: Optional[str] = None
    organiser_note: Optional[str] = None
    domestic_twin: Optional[str] = None   # "E-0042" — same event, held in Portugal
    enrichment_attempted: Optional[date] = None   # don't re-search daily for nothing
    eligible_from: Optional[date] = None  # too early now; the date that changes
    analysis: List[str] = Field(default_factory=list)   # the "why this one" note
    pinned: bool = False                  # you replied `shortlist: E-XXXX`
    applied: Optional[str] = None         # "yes" | "no", from the follow-up
    asked_applied: bool = False           # the follow-up has been put to you once
    duration_days: Optional[int] = None
    estimated_effort_hours: Optional[int] = None      # ESTIMATE
    intensity: Optional[Intensity] = None
    stages: List[Stage] = Field(default_factory=list)
    next_deadline: Optional[date] = None
    days_until_next_deadline: Optional[int] = None
    cost: str = NOT_STATED            # free | funded | stipend (X) | paid (X) | not stated
    value_justification: Optional[str] = None          # required when cost > €100
    paid_unpaid: Optional[str] = None                  # internships only
    team_requirement: str = NOT_STATED
    selectivity: str = NOT_STATED
    prerequisites: str = NOT_STATED
    age_limits: str = NOT_STATED
    rewards: str = "none stated"
    cv_value_score: Optional[int] = None               # ESTIMATE 1-5
    prestige_score: Optional[int] = None               # ESTIMATE 1-5
    cv_line: str = ""
    networking_score: Optional[int] = None             # ESTIMATE 1-5
    skills_developed: List[str] = Field(default_factory=list)
    language: str = NOT_STATED
    url: str
    source: str = ""
    http_status: Optional[int] = None                  # anti-fabrication audit trail
    confidence: Confidence = "low"
    first_seen_date: Optional[date] = None
    calendar_conflict: CalendarConflict = "none"

    # internal / rendering
    score: float = 0.0
    flags: List[str] = Field(default_factory=list)     # e.g. "worth the travel"
    estimate_notes: Dict[str, str] = Field(default_factory=dict)
    dedup_hash: str = ""
    last_seen_date: Optional[date] = None

    def price_eur(self) -> Optional[float]:
        """Parse a numeric € amount out of the cost string, if any."""
        import re
        m = re.search(r"(\d+(?:[.,]\d+)?)", self.cost or "")
        if self.cost.startswith("paid") and m:
            return float(m.group(1).replace(",", "."))
        return None

    def is_multi_day(self) -> bool:
        return bool(self.duration_days and self.duration_days > 1)
