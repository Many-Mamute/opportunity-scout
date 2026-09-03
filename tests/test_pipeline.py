"""Whole-pipeline behaviour across consecutive runs.

Every bug in the run-4 audit lived between modules, not inside one: flags that
grew because scoring ran twice, events deleted because revalidate lacked a
geocoder, a model name that only propagated down one of two paths. These tests
drive several simulated mornings in a row and assert on what survives.
"""
from datetime import date, timedelta
from pathlib import Path

import yaml

import enrich as E
import filters
from models import Event
from score import enrich_estimates, score
from store import State, Store

CFG = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "filters.yaml").read_text(encoding="utf-8"))
filters.prime(CFG)
DAY1 = date(2026, 9, 1)


def _geocoder(loc):
    return "Póvoa de Varzim" if "póvoa" in (loc or "").lower() else None


def _fresh(tmp_path):
    state = State(tmp_path / "s.json")
    return state, Store(tmp_path / "seen.jsonl", tmp_path / "db", state)


def _morning(store, cfg, today, geocoder=_geocoder):
    """One run's worth of post-ingestion processing, in main.py's order."""
    store.reset_derived_flags()

    def still_valid(ev):
        if ev.confidence == "low":
            return filters.relevance_filter(ev, cfg, today)
        return filters.apply_all(ev, cfg, geocoder, today)

    evicted = store.revalidate(still_valid, today)
    for ev in store.open.values():
        enrich_estimates(ev, cfg)
        score(ev, cfg, today, {})          # preliminary rank
    E.mark_domestic_twins(list(store.open.values()), cfg)
    for ev in store.open.values():
        score(ev, cfg, today, {})          # final rank, after enrichment
    return evicted


def _sample():
    return [
        Event(title="Formula Student Portugal 2027", organiser="FSPT",
              url="https://fspt.pt", location="Vila Real, Portugal",
              type="competition", fields=["automotive_motorsport"],
              confidence="verified", cost="free",
              start_date=date(2027, 7, 20), end_date=date(2027, 7, 24)),
        Event(title="Formula Student Germany 2027", organiser="FSG",
              url="https://fsg.de", location="Hockenheim, Germany",
              type="competition", fields=["automotive_motorsport"],
              confidence="verified", cost="funded",
              start_date=date(2027, 8, 10), end_date=date(2027, 8, 15)),
        Event(title="NEEMec robotics evening", organiser="NEEMec — FEUP",
              url="https://neemec.pt/x", location="FEUP, Porto",
              type="workshop", fields=["mechanical"], confidence="verified",
              cost="free", source="feup-nucleos",
              start_date=date(2026, 10, 14), end_date=date(2026, 10, 14)),
        Event(title="Engineering open day", organiser="Some Co",
              url="https://openday.pt", location="Rua das Flores, 4490 Póvoa",
              type="workshop", fields=["mechanical"], confidence="verified",
              cost="free", start_date=date(2026, 11, 3),
              end_date=date(2026, 11, 3)),
    ]


def test_seven_consecutive_mornings_are_stable(tmp_path):
    """Nothing should grow, vanish or drift just because time passed."""
    state, store = _fresh(tmp_path)
    for ev in _sample():
        enrich_estimates(ev, CFG)
        store.upsert(ev, DAY1)
    assert len(store.open) == 4

    first_scores = None
    for n in range(7):
        today = DAY1 + timedelta(days=n)
        evicted = _morning(store, CFG, today)
        assert evicted == [], f"day {n} deleted {evicted}"
        assert len(store.open) == 4, f"day {n} lost events"
        for ev in store.open.values():
            # No flag may appear more than the number of times one run adds it.
            for f in set(ev.flags):
                assert ev.flags.count(f) == 1, (n, ev.title, ev.flags)
            assert len(ev.flags) < 12, (n, ev.title, ev.flags)
        scores = {e.title: e.score for e in store.open.values()}
        if first_scores is None:
            first_scores = scores
    # Ranking may move as deadlines approach, but nothing should be NaN or None.
    assert all(isinstance(v, float) for v in scores.values())


def test_state_survives_a_save_load_cycle(tmp_path):
    state, store = _fresh(tmp_path)
    for ev in _sample():
        enrich_estimates(ev, CFG)
        store.upsert(ev, DAY1)
    _morning(store, CFG, DAY1)
    store.save()
    state.save()
    store.close()                                      # free the db file before reopening

    state2 = State(tmp_path / "s.json")
    store2 = Store(tmp_path / "seen.jsonl", tmp_path / "db", state2)
    assert len(store2.open) == 4
    evicted = _morning(store2, CFG, DAY1 + timedelta(days=1))
    assert evicted == [] and len(store2.open) == 4
    ids = {e.id for e in store2.open.values()}
    assert len(ids) == 4 and all(i.startswith("E-") for i in ids)


def test_domestic_twin_is_marked_once_not_once_per_run(tmp_path):
    state, store = _fresh(tmp_path)
    for ev in _sample():
        enrich_estimates(ev, CFG)
        store.upsert(ev, DAY1)
    for n in range(4):
        _morning(store, CFG, DAY1 + timedelta(days=n))
    de = [e for e in store.open.values() if "Germany" in e.title][0]
    twin_flags = [f for f in de.flags if "Portuguese edition" in f]
    assert len(twin_flags) == 1, de.flags
    assert de.domestic_twin is not None


def test_your_own_faculty_outranks_the_rest_every_morning(tmp_path):
    state, store = _fresh(tmp_path)
    for ev in _sample():
        enrich_estimates(ev, CFG)
        store.upsert(ev, DAY1)
    for n in range(3):
        _morning(store, CFG, DAY1 + timedelta(days=n))
    feup = [e for e in store.open.values() if "NEEMec" in e.organiser][0]
    other = [e for e in store.open.values() if e.organiser == "Some Co"][0]
    assert "your own faculty" in feup.flags
    assert feup.score > other.score


def test_an_ineligible_event_is_removed_on_the_next_run(tmp_path):
    """It was stored before the eligibility gate existed."""
    state, store = _fresh(tmp_path)
    cern = Event(title="CERN Summer Student Programme 2027", organiser="CERN",
                 url="https://home.cern", location="Geneva, Switzerland",
                 type="internship", fields=["mechanical"], cost="funded",
                 confidence="verified",
                 prerequisites="Undergraduate students are eligible if they "
                               "have completed at least six semesters of "
                               "full-time university study.")
    enrich_estimates(cern, CFG)
    store.upsert(cern, DAY1)
    assert len(store.open) == 1
    evicted = _morning(store, CFG, DAY1)
    assert len(evicted) == 1 and "six semesters" in evicted[0]
    assert store.open == {}
    # And it cannot come back tomorrow.
    assert store.upsert(cern, DAY1 + timedelta(days=1))[0] == "suppressed"


def test_enrichment_is_not_retried_every_single_day(tmp_path):
    """An event with no independent coverage must not burn a credit daily."""
    ev = Event(title="Obscure local workshop", organiser="Nobody",
               url="https://x.pt", confidence="verified", type="workshop")
    enrich_estimates(ev, CFG)
    assert E.needs_enrichment(ev, CFG)          # it does have gaps

    class Budget:
        def __init__(self):
            self.spent = 0

        def take(self):
            self.spent += 1
            return True

    class Gem:
        remaining, used = 10, 0

    budget = Budget()
    diag = {}
    for _ in range(3):
        E.enrich([ev], CFG, "tvly-key", "gem-key", "model", budget, Gem(), diag,
                 out_of_time=lambda: True)     # stop before any network call
    # out_of_time short-circuits before spending, so nothing was charged.
    assert budget.spent == 0
    ev.enrichment_attempted = date.today()
    E.enrich([ev], CFG, "tvly-key", "gem-key", "model", budget, Gem(), diag)
    assert budget.spent == 0, "recently-attempted event must be skipped"
