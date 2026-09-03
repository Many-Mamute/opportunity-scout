"""Academic-eligibility gating, and regressions for three pipeline bugs found
in the run-4 audit."""
from datetime import date, timedelta
from pathlib import Path

import yaml

import filters
from models import STICKY_FLAGS, Event
from score import enrich_estimates, score
from store import State, Store

CFG = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "filters.yaml").read_text(encoding="utf-8"))
filters.prime(CFG)
TODAY = date(2026, 8, 31)


def _ev(prereq="", **kw):
    base = dict(title="X", url="https://x", organiser="Org", location="Porto",
                confidence="verified", type="summer_school", fields=["mechanical"],
                prerequisites=prereq)
    base.update(kw)
    return Event(**base)


# ------------------------------------------------------ semester arithmetic
def test_completed_semesters_tracks_the_feup_calendar():
    f = filters.completed_semesters
    assert f(date(2026, 12, 1), CFG) == 0      # mid first semester
    assert f(date(2027, 2, 5), CFG) == 1       # first exam period done
    assert f(date(2027, 7, 13), CFG) == 2      # first year complete
    assert f(date(2028, 7, 13), CFG) == 4
    assert f(date(2031, 7, 13), CFG) == 10     # capped at the degree length


# --------------------------------------------------- reading the bar out of prose
def test_the_cern_sentence_is_understood():
    """This exact wording was printed on a card and recommended anyway."""
    text = ("Undergraduate students are eligible if they have completed at "
            "least six semesters of full-time university study before the "
            "programme begins and continue to be enrolled throughout.")
    need, phrase = filters.required_semesters(text, CFG)
    assert need == 6 and "six semesters" in phrase


def test_other_phrasings():
    cases = [
        ("Applicants must have completed at least two years of university studies.", 4),
        ("Open to third-year students and above.", 4),
        ("Open to 3rd year students.", 4),
        ("For 2nd year undergraduates.", 2),
        ("For final-year students only.", 8),
        ("Reserved for penultimate year students.", 6),
        ("Aberto a estudantes do terceiro ano.", 4),
        ("Os candidatos devem ter concluído pelo menos quatro semestres do curso.", 4),
    ]
    for text, want in cases:
        need, _ = filters.required_semesters(text, CFG)
        assert need == want, f"{text!r} -> {need}, expected {want}"


def test_no_bar_means_no_bar():
    for text in [
        "Open to all students, no experience necessary.",
        "For first-year students at FEUP.",
        "Teams of four. Two days of building. Prizes awarded.",
        "Celebrating 25 years of tradition in Porto.",
        "The second edition of our annual challenge.",
        "Participants must be at least 18 years old.",
        "",
    ]:
        need, _ = filters.required_semesters(text, CFG)
        assert need is None, f"false positive on {text!r} -> {need}"


# ----------------------------------------------------------- the gate itself
def test_cern_is_now_rejected_with_an_honest_reason():
    ev = _ev("Undergraduate students are eligible if they have completed at "
             "least six semesters of full-time university study.",
             title="CERN Summer Student Programme 2027", organiser="CERN",
             start_date=date(2027, 6, 21), end_date=date(2027, 8, 20))
    ok, why = filters.eligibility_filter(ev, CFG, TODAY)
    assert not ok
    assert "requires 6 completed semesters" in why and "you will have 1" in why
    assert any("needs 6 semesters" in f for f in ev.flags)


def test_an_event_you_will_have_grown_into_is_kept():
    """Same bar, but far enough away that you meet it."""
    ev = _ev("Applicants must have completed at least two years of university "
             "studies.", start_date=date(2028, 7, 20), end_date=date(2028, 8, 20))
    ok, _ = filters.eligibility_filter(ev, CFG, TODAY)
    assert ok


def test_undated_events_are_judged_a_year_out_not_skipped():
    """The CERN card said 'Dates not stated' — the check must still bite."""
    ev = _ev("Requires at least six semesters of university study.")
    assert ev.start_date is None
    ok, why = filters.eligibility_filter(ev, CFG, TODAY)
    assert not ok and "you will have" in why


def test_eligibility_is_wired_into_the_pipeline():
    # After the June/July exam period, so the calendar filter is not what
    # rejects it — the eligibility gate must be the thing that bites.
    ev = _ev("Open to students who have completed at least six semesters.",
             start_date=date(2027, 7, 20), end_date=date(2027, 7, 30), cost="free")
    enrich_estimates(ev, CFG)
    ok, why = filters.apply_all(ev, CFG, lambda l: "Porto", TODAY)
    assert not ok and "eligibility" in why


# ============================ regressions for the run-4 audit ================
def test_bug_a_derived_flags_do_not_accumulate(tmp_path):
    """score() runs twice per run now; flags persist to seen.jsonl."""
    state = State(tmp_path / "s.json")
    store = Store(tmp_path / "seen.jsonl", tmp_path / "db", state)
    ev = Event(title="Robotics night", organiser="NEEMec — FEUP",
               url="https://neemec.pt/x", type="workshop", fields=["mechanical"],
               source="feup-nucleos", confidence="verified")
    enrich_estimates(ev, CFG)
    store.upsert(ev, TODAY)
    for _ in range(3):                       # three mornings
        store.reset_derived_flags()
        for e in store.open.values():
            score(e, CFG, TODAY, {})
            score(e, CFG, TODAY, {})         # prelim + final, as main does
    got = list(store.open.values())[0].flags
    # Once per morning, not once per score() call and not six times over three
    # mornings — the whole point of reset_derived_flags.
    assert got.count("your own faculty") == 1, got


def test_bug_a_user_set_flags_survive_the_reset(tmp_path):
    state = State(tmp_path / "s.json")
    store = Store(tmp_path / "seen.jsonl", tmp_path / "db", state)
    ev = Event(title="X", organiser="Org", url="https://x", confidence="verified")
    store.upsert(ev, TODAY)
    stored = list(store.open.values())[0]
    stored.flags = ["you marked this interesting", "clashes with term time"]
    store.reset_derived_flags()
    assert stored.flags == ["you marked this interesting"]
    assert "you marked this interesting" in STICKY_FLAGS


def test_bug_b_reject_night_online_knob_actually_works():
    cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" /
                          "filters.yaml").read_text(encoding="utf-8"))
    filters.prime(cfg)
    def night():
        return Event(title="X", url="https://x", format="online",
                     location="online", start_time="02:00", tz_known=True)
    cfg["settings"]["reject_night_online"] = False
    ok, _ = filters.geography_filter(night(), cfg)
    assert ok, "default must keep it and let you decide"
    cfg["settings"]["reject_night_online"] = True
    ok, why = filters.geography_filter(night(), cfg)
    assert not ok and "02:00 Lisbon" in why
    filters.prime(CFG)


def test_bug_c_revalidate_with_a_geocoder_keeps_geocoded_events(tmp_path):
    """Without a geocoder, every Nominatim-resolved event was silently
    deleted on the next run."""
    state = State(tmp_path / "s.json")
    store = Store(tmp_path / "seen.jsonl", tmp_path / "db", state)
    ev = Event(title="Engineering open day", organiser="Some Co", url="https://y",
               location="Rua das Flores 12, 4490 Póvoa", type="workshop",
               fields=["mechanical"], confidence="verified", cost="free",
               start_date=date(2026, 11, 3), end_date=date(2026, 11, 3))
    enrich_estimates(ev, CFG)
    store.upsert(ev, TODAY)

    geocoder = lambda loc: "Póvoa de Varzim"
    evicted = store.revalidate(
        lambda e: filters.apply_all(e, CFG, geocoder, TODAY), TODAY)
    assert evicted == [] and len(store.open) == 1

    # And the old behaviour is what would have destroyed it.
    ok, why = filters.apply_all(ev, CFG, None, TODAY)
    assert not ok and "unverifiable" in why
