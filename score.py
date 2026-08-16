"""Estimates (the only four permitted) and the composite ranking.

composite = 0.28·cv + 0.20·field_match + 0.18·deadline_urgency
          + 0.14·networking + 0.12·competition_bonus + 0.08·prestige
(weights from config). Advantageous stages add a flat bonus. A distant
deadline must not sink a high-prestige item: with urgency at zero, full
cv+prestige alone still clears the top-20 bar in practice.
"""
from __future__ import annotations

from datetime import date
from typing import Dict

from models import Event
from store import normalise


def enrich_estimates(ev: Event, cfg: dict) -> None:
    est = cfg["estimates"]
    label = ev.type.replace("_", " ")
    article = "an" if label[:1] in "aeiou" else "a"
    if ev.estimated_effort_hours is None:
        per_day = est["hours_per_day"].get(ev.type, est["hours_per_day"]["other"])
        if ev.type == "internship":
            weeks = max(1, (ev.duration_days or 30) // 7)
            ev.estimated_effort_hours = int(weeks * per_day)
            src = ("the stated dates" if ev.duration_days
                   else "a 30-day assumption, because no dates were stated")
            ev.estimate_notes["effort"] = (
                f"Effort {ev.estimated_effort_hours}h: internships are counted "
                f"in weeks — {weeks} week(s) from {src}, at {per_day}h per week.")
        else:
            days = ev.duration_days or 1
            ev.estimated_effort_hours = int(days * per_day)
            src = ("" if ev.duration_days
                   else " (no dates stated, so assumed a single day)")
            ev.estimate_notes["effort"] = (
                f"Effort {ev.estimated_effort_hours}h: {days} day(s) × {per_day}h, "
                f"the table value for {article} {label}{src}.")
    h = ev.estimated_effort_hours
    ev.intensity = ("light" if h < 5 else "moderate" if h < 20
                    else "heavy" if h < 60 else "intense")

    # Every score below is a lookup table, not judgement. The notes say exactly
    # which row of which table produced the number, so a wrong score points at
    # the line of config to change.
    default_prestige = est["base_prestige_default"]
    if ev.prestige_score is None:
        org = normalise(ev.organiser)
        boost, why = default_prestige, None
        for key, val in est["prestige_boost"].items():
            if key in org and int(val) >= boost:
                boost = int(val)
                why = (f"the organiser name contains \"{key}\", which the "
                       f"prestige table scores {boost}")
        for pres in cfg["override"]["prestigious_organisers"]:
            if normalise(pres) in org:
                boost = 5
                why = (f"\"{pres}\" is on the prestigious-organisers list, "
                       f"which always scores 5")
        ev.prestige_score = boost
        ev.estimate_notes["prestige"] = (
            f"Prestige {boost}/5: " + (why + "." if why else
            f"the scout does not recognise \"{ev.organiser}\", and unknown "
            f"organisers default to {default_prestige}. This is name-matching "
            f"only — a low score here means unrecognised, not unimpressive."))
    if ev.cv_value_score is None:
        base = est["base_cv"].get(ev.type, 2)
        why = (f"CV {{}}/5: {article} {label} starts at {base} on the event-type table")
        if ev.prestige_score >= 4:
            base = min(5, base + 1)
            why += ", plus 1 because the organiser scored 4 or more on prestige"
        else:
            why += ("; no organiser bonus, since that needs a prestige score "
                    "of 4 or more")
        ev.cv_value_score = base
        ev.estimate_notes["cv"] = why.format(base) + "."
    if ev.networking_score is None:
        ev.networking_score = est["base_networking"].get(ev.type, 2)
        ev.estimate_notes["networking"] = (
            f"Networking {ev.networking_score}/5: the flat table value for "
            f"{article} "
            f"{label}. Nothing about who actually attends this particular "
            f"event was assessed.")

    if not ev.cv_line:
        yr = (ev.start_date or ev.first_seen_date or date.today()).year
        ev.cv_line = f"{ev.title} — {ev.organiser} ({ev.type.replace('_', ' ')}, {yr})"
    if not ev.skills_developed:
        ev.skills_developed = {
            "hackathon": ["prototyping", "teamwork", "pitching"],
            "competition": ["problem solving", "teamwork"],
            "internship": ["professional experience"],
            "summer_school": ["technical depth", "networking"],
        }.get(ev.type, [])


def compute_deadlines(ev: Event, today: date) -> None:
    future = [s.closes for s in ev.stages if s.closes and s.closes >= today]
    ev.next_deadline = min(future) if future else ev.next_deadline
    ev.days_until_next_deadline = ((ev.next_deadline - today).days
                                   if ev.next_deadline else None)


def deadline_urgency(days) -> float:
    """Peaks 3–14 days out."""
    if days is None:
        return 0.0
    if days < 0:
        return 0.0
    if days < 3:
        return 0.8
    if days <= 14:
        return 1.0
    if days <= 30:
        return 1.0 - 0.7 * (days - 14) / 16
    return 0.15


def downweight_multiplier(ev: Event, downweights: Dict[str, int],
                          cfg: dict) -> float:
    sc = cfg["scoring"]
    factor, floor = sc["downweight_factor"], sc["downweight_floor"]
    ceiling = sc.get("upweight_ceiling", 2.5)
    keys = [f"type:{ev.type}", f"organiser:{normalise(ev.organiser)}"]
    keys += [f"field:{f}" for f in ev.fields]
    mult = 1.0
    for k in keys:
        # A negative count means you said "interested" — same lever, other way.
        mult *= factor ** downweights.get(k, 0)
    return min(ceiling, max(floor, mult))


def score(ev: Event, cfg: dict, today: date,
          downweights: Dict[str, int] | None = None) -> float:
    w = cfg["scoring"]["weights"]
    compute_deadlines(ev, today)
    fw = cfg["scoring"]["field_weights"]
    field_match = max((fw.get(f, 0.0) for f in ev.fields), default=0.0)
    comp_bonus = 1.0 if ev.type in ("competition", "hackathon") else 0.0
    s = (w["cv_value"] * (ev.cv_value_score or 0) / 5
         + w["field_match"] * field_match
         + w["deadline_urgency"] * deadline_urgency(ev.days_until_next_deadline)
         + w["networking"] * (ev.networking_score or 0) / 5
         + w["competition_bonus"] * comp_bonus
         + w["prestige"] * (ev.prestige_score or 0) / 5)
    if any(s_.is_advantageous and (not s_.closes or s_.closes >= today)
           for s_ in ev.stages):
        s += cfg["scoring"]["advantageous_stage_bonus"]
    s *= downweight_multiplier(ev, downweights or {}, cfg)
    ev.score = round(s, 4)
    return ev.score
