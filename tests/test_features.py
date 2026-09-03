"""Shortlist, apply follow-through, seasonal recurrence, and the analysis note."""
from datetime import date, timedelta
from pathlib import Path

import yaml

import analysis
import feedback as F
import filters
import recurrence
from models import Event, Stage
from score import enrich_estimates, score
from store import State, Store

CFG = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "filters.yaml").read_text(encoding="utf-8"))
filters.prime(CFG)
TODAY = date(2026, 9, 1)


def _store(tmp_path):
    state = State(tmp_path / "s.json")
    return state, Store(tmp_path / "seen.jsonl", tmp_path / "db", state)


def _mk(**kw):
    base = dict(title="Porto Engineering Challenge", organiser="Org",
                url="https://x", type="competition", fields=["mechanical"],
                confidence="verified", location="Porto", cost="free")
    base.update(kw)
    e = Event(**base)
    enrich_estimates(e, CFG)
    return e


# --------------------------------------------------------------- shortlist
def test_shortlist_pins_and_survives_a_reload(tmp_path):
    state, store = _store(tmp_path)
    e = _mk()
    store.upsert(e, TODAY)
    F.apply_feedback(F.parse_body(f"shortlist: {e.id}"), store, state, CFG, TODAY)
    assert store.find_by_id(e.id).pinned is True
    store.save()
    state.save()
    store.close()                                      # free the db file before reopening

    state2 = State(tmp_path / "s.json")
    store2 = Store(tmp_path / "seen.jsonl", tmp_path / "db", state2)
    assert store2.find_by_id(e.id).pinned is True      # a pin is not a flag

    F.apply_feedback(F.parse_body(f"unpin: {e.id}"), store2, state2, CFG, TODAY)
    assert store2.find_by_id(e.id).pinned is False


def test_pin_is_not_cleared_by_the_derived_flag_reset(tmp_path):
    state, store = _store(tmp_path)
    e = _mk()
    store.upsert(e, TODAY)
    F.apply_feedback(F.parse_body(f"pin: {e.id}"), store, state, CFG, TODAY)
    store.reset_derived_flags()
    assert store.find_by_id(e.id).pinned is True


def test_shortlist_accepts_several_ids():
    p = F.parse_body("shortlist: E-0004, E-0011")
    assert p["shortlist"] == ["E-0004", "E-0011"]


# ------------------------------------------------------- apply follow-through
def test_applied_and_skipped_are_recorded(tmp_path):
    state, store = _store(tmp_path)
    a, b = _mk(url="https://a"), _mk(title="Другое", url="https://b")
    store.upsert(a, TODAY)
    store.upsert(b, TODAY)
    F.apply_feedback(F.parse_body(f"applied: {a.id}"), store, state, CFG, TODAY)
    F.apply_feedback(F.parse_body(f"skipped: {b.id}"), store, state, CFG, TODAY)
    assert store.find_by_id(a.id).applied == "yes"
    assert store.find_by_id(b.id).applied == "no"


def test_didnt_apply_is_understood():
    assert F.parse_body("didn't apply: E-0007")["skipped"] == ["E-0007"]


def test_the_question_is_asked_once_only(tmp_path):
    """asked_applied is sticky, so the same nag cannot repeat every morning."""
    state, store = _store(tmp_path)
    e = _mk(next_deadline=TODAY + timedelta(days=1))
    store.upsert(e, TODAY)
    stored = store.find_by_id(e.id)
    stored.flags.append("you marked this interesting")
    assert stored.asked_applied is False
    stored.asked_applied = True
    store.reset_derived_flags()
    assert store.find_by_id(e.id).asked_applied is True


# ---------------------------------------------------------------- recurrence
def test_an_annual_event_is_remembered_and_predicted():
    state = {}
    ev = _mk(title="CERN Summer Student Programme 2027", organiser="CERN",
             url="https://home.cern",
             stages=[Stage(name="Applications", opens=date(2026, 11, 1),
                           closes=date(2027, 1, 31))])
    assert recurrence.record([ev], state, date(2026, 11, 2)) == 1

    # Nothing is open a year later, but the anniversary is approaching.
    prompts = recurrence.as_prompts(state, date(2027, 10, 20), open_keys=set())
    assert prompts and "CERN" in prompts[0]
    assert "2026" in prompts[0], "must say which year it is extrapolating from"


def test_no_prediction_while_the_real_thing_is_open():
    state = {}
    ev = _mk(title="CERN Summer Student Programme 2027", organiser="CERN",
             url="https://home.cern",
             stages=[Stage(name="Applications", opens=date(2026, 11, 1),
                           closes=date(2027, 1, 31))])
    recurrence.record([ev], state, date(2026, 11, 2))
    key = list(state["recurrence"].keys())[0]   # nested under "recurrence"
    assert recurrence.as_prompts(state, date(2027, 10, 20), open_keys={key}) == []


def test_no_prediction_far_from_the_anniversary():
    state = {}
    ev = _mk(title="Annual Thing", url="https://t",
             stages=[Stage(name="Applications", opens=date(2026, 11, 1))])
    recurrence.record([ev], state, date(2026, 11, 2))
    assert recurrence.as_prompts(state, date(2027, 4, 1), open_keys=set()) == []


# ------------------------------------------------------------ analysis note
def test_every_event_gets_a_note():
    for ev in [_mk(), _mk(type="internship"), _mk(type="workshop"),
               _mk(type="other", fields=[])]:
        analysis.annotate(ev, CFG, TODAY)
        assert ev.analysis and len(" ".join(ev.analysis)) > 60


def test_toastmasters_and_debate_are_described_differently():
    tm = _mk(title="Toastmasters Porto — open evening", type="workshop",
             fields=["public_speaking"])
    db = _mk(title="Porto Debating Championship", type="competition",
             fields=["public_speaking"])
    analysis.annotate(tm, CFG, TODAY)
    analysis.annotate(db, CFG, TODAY)
    tm_text, db_text = " ".join(tm.analysis), " ".join(db.analysis)
    assert "not a contest" in tm_text and "repetition over months" in tm_text
    assert "adversarial" in db_text.lower()
    assert tm_text != db_text
    # Each must set expectations about what it is actually like.
    assert "Table Topic" in tm_text
    assert "Parliamentary" in db_text or "seven-minute" in db_text


def test_price_is_stated_plainly_in_the_note():
    paid = _mk(cost="paid (€150)")
    filters.cost_filter(paid, CFG)
    analysis.annotate(paid, CFG, TODAY)
    text = " ".join(paid.analysis)
    assert "€150" in text
    assert "sceptic" in text.lower() or "what you get" in text.lower()

    cheap = _mk(cost="paid (€20)")
    analysis.annotate(cheap, CFG, TODAY)
    assert "€20" in " ".join(cheap.analysis)


def test_note_names_skills():
    ev = _mk(type="hackathon", fields=["mechanical", "data_ai"])
    analysis.annotate(ev, CFG, TODAY)
    assert ev.skills_developed
    assert any("prototyp" in s for s in ev.skills_developed)


def test_note_flags_the_domestic_twin():
    ev = _mk(title="Formula Student Germany 2027", location="Hockenheim, Germany")
    ev.domestic_twin = "E-0001"
    analysis.annotate(ev, CFG, TODAY)
    assert "Portuguese edition" in " ".join(ev.analysis)


# ------------------------------------------- projected academic calendar
def test_future_years_are_projected_and_labelled():
    """The config holds 2026/27 only; without projection a heavy event in
    January 2028 sees no exam period at all."""
    from models import Event as E
    real = E(title="Heavy", url="https://x", estimated_effort_hours=30,
             start_date=date(2027, 1, 15), end_date=date(2027, 1, 15))
    ok, _ = filters.calendar_filter(real, CFG)
    assert not ok and "term dates projected, not published" not in real.flags

    projected = E(title="Heavy", url="https://x", estimated_effort_hours=30,
                  start_date=date(2028, 1, 15), end_date=date(2028, 1, 15))
    ok, _ = filters.calendar_filter(projected, CFG)
    assert not ok, "a projected exam period must still protect you"
    assert "term dates projected, not published" in projected.flags


def test_projection_is_idempotent():
    """prime() runs on every import and every run."""
    cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" /
                          "filters.yaml").read_text(encoding="utf-8"))
    filters.prime(cfg)
    n = len(cfg["calendar"]["exam_periods"])
    for _ in range(5):
        filters.prime(cfg)
    assert len(cfg["calendar"]["exam_periods"]) == n


def test_a_date_in_the_published_year_is_not_called_provisional():
    assert filters.calendar_is_provisional(date(2027, 1, 15), CFG) is False
    assert filters.calendar_is_provisional(date(2028, 1, 15), CFG) is True
    assert filters.calendar_is_provisional(None, CFG) is False


# ============================ deferred eligibility (idea 5) ==================
def _cern():
    return _mk(title="CERN Summer Student Programme 2027", organiser="CERN",
               url="https://home.cern", type="internship", cost="funded",
               location="Geneva, Switzerland",
               prerequisites="Undergraduate students are eligible if they have "
                             "completed at least six semesters of full-time "
                             "university study.")


def test_eligible_from_is_computed():
    assert filters.eligible_from(2, CFG) == date(2027, 7, 13)
    assert filters.eligible_from(6, CFG) == date(2029, 7, 13)
    assert filters.eligible_from(99, CFG) is None      # longer than the degree


def test_too_early_is_parked_not_deleted(tmp_path):
    state, store = _store(tmp_path)
    ev = _cern()
    ok, why = filters.apply_all(ev, CFG, lambda l: None, TODAY)
    assert not ok and why.startswith("eligibility")
    assert ev.eligible_from == date(2029, 7, 13)
    assert store.defer(ev, TODAY) == "deferred"
    assert store.open == {} and len(store.deferred) == 1


def test_deferred_events_survive_a_reload(tmp_path):
    state, store = _store(tmp_path)
    ev = _cern()
    filters.apply_all(ev, CFG, lambda l: None, TODAY)
    store.defer(ev, TODAY)
    store.save()
    state.save()
    store.close()                                      # free the db file before reopening

    state2 = State(tmp_path / "s.json")
    store2 = Store(tmp_path / "seen.jsonl", tmp_path / "db", state2)
    assert len(store2.deferred) == 1 and store2.open == {}
    kept = list(store2.deferred.values())[0]
    assert kept.eligible_from == date(2029, 7, 13)
    assert kept.title.startswith("CERN")


def test_it_returns_by_itself_when_the_date_arrives(tmp_path):
    state, store = _store(tmp_path)
    ev = _cern()
    filters.apply_all(ev, CFG, lambda l: None, TODAY)
    store.defer(ev, TODAY)
    assert store.release_eligible(date(2029, 7, 12)) == []      # one day early
    released = store.release_eligible(date(2029, 7, 13))
    assert len(released) == 1 and store.deferred == {}
    assert "you are now eligible for this" in released[0].flags
    assert len(store.open) == 1


def test_only_the_approaching_ones_are_mentioned(tmp_path):
    state, store = _store(tmp_path)
    ev = _cern()
    filters.apply_all(ev, CFG, lambda l: None, TODAY)
    store.defer(ev, TODAY)
    assert store.upcoming_eligibility(TODAY, within_days=240) == []
    soon = store.upcoming_eligibility(date(2029, 3, 1), within_days=240)
    assert len(soon) == 1


def test_saying_no_beats_deferral(tmp_path):
    """An explicit rejection must not be resurrected by an eligibility date."""
    state, store = _store(tmp_path)
    ev = _cern()
    store.upsert(ev, TODAY)
    store.dismiss(store.find_by_id(ev.id), "uninterested", TODAY)
    fresh = _cern()
    filters.apply_all(fresh, CFG, lambda l: None, TODAY)
    assert store.defer(fresh, TODAY) == "suppressed"
    assert store.deferred == {}


# ============================ quiet-week fallback (idea 4) ===================
def test_fallback_queries_exist_and_are_opt_in():
    src = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" /
                          "sources.yaml").read_text(encoding="utf-8"))["sources"]
    fb = [s for s in src if s.get("fallback")]
    assert fb, "no fallback source defined"
    assert all(s["type"] == "tavily" for s in fb)
    assert sum(len(s["queries"]) for s in fb) >= 4
    # They must not be swept every day, only when a week is quiet.
    normal = [s for s in src if s.get("type") == "tavily" and not s.get("fallback")]
    assert normal, "the daily sweep must still exist separately"


def test_thin_threshold_is_configured():
    assert CFG["settings"]["thin_digest_threshold"] >= 1
    assert CFG["settings"]["eligibility_horizon_days"] >= 30


def test_budget_stays_inside_the_free_tier_even_on_a_quiet_week():
    src = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" /
                          "sources.yaml").read_text(encoding="utf-8"))["sources"]
    daily = sum(len(s.get("queries", [])) for s in src
                if s.get("type") == "tavily" and s.get("enabled", True)
                and not s.get("fallback"))
    fb = sum(len(s.get("queries", [])) for s in src if s.get("fallback"))
    worst = daily + CFG["settings"]["enrichment_max_events"] + fb
    assert worst <= CFG["settings"]["tavily_daily_cap"]
    assert worst * 30 <= 1000, "a month of worst-case runs must fit the free tier"
