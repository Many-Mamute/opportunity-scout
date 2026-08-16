from datetime import date
from pathlib import Path

import yaml

import filters
from extract import parse_jsonld
from models import Event
from score import enrich_estimates, score
from store import State, Store

CFG = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "filters.yaml").read_text())
filters.prime(CFG)
TODAY = date(2026, 8, 13)


def ev(**kw):
    base = dict(title="X", url="https://x", organiser="Org")
    base.update(kw)
    return Event(**base)


# ---------------------------------------------------------------- triage
def test_triage():
    assert filters.triage("Estágio de Verão Bosch", "https://b", "", CFG)
    assert not filters.triage("Executive Education MBA", "https://b", "", CFG)
    assert not filters.triage("Quarterly results call", "https://b", "", CFG)


# ------------------------------------------------------------- geography
def test_single_day_braga_allowed_multi_day_rejected():
    ok, _ = filters.geography_filter(
        ev(location="Braga", start_date=TODAY, end_date=TODAY, duration_days=1), CFG)
    assert ok
    ok, reason = filters.geography_filter(
        ev(location="Braga", duration_days=3), CFG)
    assert not ok and "multi-day" in reason


def test_multi_day_porto_ok_and_travel_minutes():
    e = ev(location="FEUP, Porto", duration_days=3)
    ok, _ = filters.geography_filter(e, CFG)
    assert ok and e.travel_time_from_porto_min == 0


def test_lisbon_rejected_without_override():
    ok, reason = filters.geography_filter(ev(location="Lisboa", duration_days=1), CFG)
    assert not ok


def test_funded_or_prestigious_override_anywhere():
    e = ev(title="CERN Summer Student Programme", organiser="CERN",
           location="Geneva, Switzerland", duration_days=60, cost="funded")
    ok, _ = filters.geography_filter(e, CFG)
    assert ok and "worth the travel" in e.flags


def test_online_night_sessions_rejected():
    e = ev(format="online", location="online",
           one_line_summary="Live sessions daily at 03:00 UTC")
    ok, reason = filters.geography_filter(e, CFG)
    assert not ok and "00:00" in reason


# -------------------------------------------------------------- calendar
def test_heavy_event_too_close_to_january_exams_blocked():
    e = ev(start_date=date(2026, 12, 26), end_date=date(2026, 12, 28),
           estimated_effort_hours=30)
    ok, reason = filters.calendar_filter(e, CFG)
    assert not ok and e.calendar_conflict == "blocked"


def test_heavy_event_ending_by_dec_21_allowed():
    e = ev(start_date=date(2026, 12, 3), end_date=date(2026, 12, 5),
           estimated_effort_hours=30)
    ok, _ = filters.calendar_filter(e, CFG)
    assert ok


def test_late_july_heavy_event_unaffected():
    e = ev(start_date=date(2027, 7, 20), end_date=date(2027, 7, 30),
           estimated_effort_hours=60)
    ok, _ = filters.calendar_filter(e, CFG)
    assert ok


def test_exam_period_only_light_allowed():
    light = ev(start_date=date(2027, 1, 10), end_date=date(2027, 1, 10),
               estimated_effort_hours=3)
    ok, _ = filters.calendar_filter(light, CFG)
    assert ok and light.calendar_conflict == "near_exams"
    moderate = ev(start_date=date(2027, 1, 10), end_date=date(2027, 1, 10),
                  estimated_effort_hours=10)
    ok, _ = filters.calendar_filter(moderate, CFG)
    assert not ok and moderate.calendar_conflict == "blocked"


def test_term_time_light_event_flagged_not_rejected():
    e = ev(start_date=date(2026, 10, 14), end_date=date(2026, 10, 14),
           estimated_effort_hours=6)
    ok, _ = filters.calendar_filter(e, CFG)
    assert ok and e.calendar_conflict == "clashes_with_term_time"
    assert "clashes with term time" in e.flags


# ----------------------------------------------------------------- level
def test_level_rejects_phd_and_experience():
    ok, _ = filters.level_filter(ev(one_line_summary="PhD students only"), CFG)
    assert not ok
    ok, _ = filters.level_filter(ev(prerequisites="5+ years of experience"), CFG)
    assert not ok
    e = ev(one_line_summary="Open to all, under-21 category with its own prize")
    ok, _ = filters.level_filter(e, CFG)
    assert ok and "age-category advantage" in e.flags


# ------------------------------------------------------------------ cost
def test_cost_rules():
    ok, _ = filters.cost_filter(ev(cost="paid (€650)"), CFG)
    assert not ok
    justified = ev(cost="paid (€150)", rewards="certificate, ECTS",
                   prestige_score=4)
    ok, _ = filters.cost_filter(justified, CFG)
    assert ok and justified.value_justification and "€150" in justified.value_justification
    ok, _ = filters.cost_filter(ev(cost="paid (€150)"), CFG)  # no stated basis
    assert not ok


def test_certificate_mill_rejected():
    e = ev(title="Self-paced certificate course", cost="paid (€49)",
           one_line_summary="Learn at your own pace, lifetime access",
           organiser="not stated")
    ok, reason = filters.cost_filter(e, CFG)
    assert not ok and "mill" in reason


# ---------------------------------- end-to-end: one Tier 1 fixture source
def test_fixture_end_to_end_through_pipeline(tmp_path):
    html = (Path(__file__).parent / "fixtures" / "eventbrite_event.html").read_text()
    [e] = parse_jsonld(html, "https://www.eventbrite.com/e/x", "eventbrite-porto", 200)
    filters.classify(e, CFG, html)
    enrich_estimates(e, CFG)
    ok, reason = filters.apply_all(e, CFG)
    assert ok, reason
    assert e.type == "hackathon" and "mechanical" in e.fields
    assert e.team_requirement == "team_required"
    assert "certificate" in e.rewards
    store = Store(tmp_path / "seen.jsonl", tmp_path / "db", State(tmp_path / "s.json"))
    status, canonical = store.upsert(e, TODAY)
    assert status == "new" and canonical.id == "E-0001"
    s = score(canonical, CFG, TODAY)
    assert s > 0.4                       # competition bonus + field match + cv
    assert canonical.next_deadline == date(2026, 11, 1)
    assert canonical.estimate_notes            # estimates are labelled


# --------------------------------------------- relevance gate (the noise fix)
def _real(**kw):
    """Cases drawn verbatim from the first live digest."""
    base = dict(url="https://x", organiser="Org", confidence="verified",
                start_date=date(2026, 8, 20), end_date=date(2026, 8, 20))
    base.update(kw)
    return Event(**base)


def test_social_events_from_the_first_run_are_rejected():
    noise = [
        ("Free International PUBCRAWL w/ Local Guides!", ""),
        ("Online Speed Dating | For UK Indian Singles (Ages 27 - 40)", ""),
        ("SOCIAL Boardgames: Play and meet new friends!", ""),
        ("Why Aren't You Happy Yet? - By a Psychic Reader & a Hypnotherapist", ""),
        ("Crafting & Craft Beer Night", ""),
        ("Quiz & Beer Night / Noite de Quiz e Cerveja", ""),
        ("Porto 10000 steps walk", ""),
        ("MEANINGFUL MEAL: Insightful Dinner with New Friends", ""),
        ("Biz & Buzz: Your After-Work Connection in Porto",
         "PBN's intentional, low-key networking"),
        ("Porto Future Tech - Casual Networking", ""),
    ]
    for title, summary in noise:
        e = _real(title=title, one_line_summary=summary)
        filters.classify(e, CFG)
        ok, reason = filters.relevance_filter(e, CFG, TODAY)
        assert not ok, f"should have been rejected: {title}"


def test_retrospective_snippets_rejected():
    for title in [
        "André Zandamela Jr. — ESTÁGIO CONCLUÍDO! Tive a honra de realizar",
        "Formula Student Portugal: FSPT23 - Award Ceremony Video",
        "openBIM Hackathon: Porto 2023 - Building Innovation",
        "I first heard of Formula Student in October 2012 during a lunch",
    ]:
        e = _real(title=title, confidence="low", type="competition",
                  fields=["automotive_motorsport"], start_date=None, end_date=None)
        ok, reason = filters.relevance_filter(e, CFG, TODAY)
        assert not ok, f"should have been rejected: {title}"


def test_genuine_opportunities_survive_the_gate():
    keep = [
        _real(title="Porto Robotics Hackathon 2026", type="hackathon",
              fields=["mechanical"]),
        _real(title="KTH Formula Student recruiting for the 2026/27 season",
              confidence="low", type="competition",
              fields=["automotive_motorsport"], start_date=None, end_date=None),
        _real(title="CERN Summer Student Programme 2027", type="summer_school",
              fields=["mechanical"], cost="funded"),
        _real(title="Estágio de Verão Bosch — Engenharia Mecânica",
              type="internship", fields=["mechanical"]),
    ]
    for e in keep:
        ok, reason = filters.relevance_filter(e, CFG, TODAY)
        assert ok, f"should have been kept: {e.title} ({reason})"


def test_past_events_rejected():
    e = _real(title="Porto Engineering Challenge", type="competition",
              fields=["mechanical"], start_date=date(2026, 8, 1),
              end_date=date(2026, 8, 2))
    ok, reason = filters.relevance_filter(e, CFG, TODAY)
    assert not ok and "already started" in reason
