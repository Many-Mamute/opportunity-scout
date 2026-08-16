"""Regressions from the third live run: times, retroactive filtering, and the
expandable roundup."""
from datetime import date
from pathlib import Path

import yaml

import email_render as R
import filters
from extract import parse_jsonld, _to_lisbon
from models import Event
from score import enrich_estimates
from store import State, Store

CFG = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "filters.yaml").read_text())
filters.prime(CFG)
TODAY = date(2026, 8, 16)


# --------------------------------------------------------------------- times
def test_times_are_converted_to_lisbon():
    d, t, known, src = _to_lisbon("2026-08-20T19:00:00-06:00")
    assert (d, t, known, src) == (date(2026, 8, 21), "02:00", True, "UTC-06:00")


def test_bare_date_invents_no_time():
    assert _to_lisbon("2026-08-22") == (date(2026, 8, 22), None, False, None)


def test_when_row_shows_lisbon_time():
    e = Event(title="X", url="https://x", start_date=date(2026, 8, 20),
              end_date=date(2026, 8, 20), start_time="18:30", end_time="21:00",
              tz_known=True, tz_source="UTC+01:00")
    s = R._when(e, TODAY)
    assert "18:30–21:00" in s and "Lisbon time" in s


def test_unstated_timezone_is_labelled_not_assumed():
    e = Event(title="X", url="https://x", start_date=date(2026, 8, 20),
              end_date=date(2026, 8, 20), start_time="11:30", tz_known=False)
    assert "no timezone stated" in R._when(e, TODAY)


def test_online_event_at_night_in_lisbon_is_rejected():
    """The Guatemala workshop: 19:00 UTC-6 is 02:00 here."""
    e = Event(title="Cursor Guatemala Workshop", url="https://x", format="online",
              location="online", start_date=date(2026, 8, 21),
              start_time="02:00", tz_known=True, tz_source="UTC-06:00")
    ok, reason = filters._online_hours(e)
    assert not ok and "02:00 Lisbon" in reason


def test_jsonld_end_to_end_carries_times():
    html = (Path(__file__).parent / "fixtures" / "eventbrite_event.html").read_text()
    [e] = parse_jsonld(html, "https://x", "src", 200)
    assert e.start_time == "09:00" and e.tz_known


# --------------------------------------------- retroactive filtering (the bug)
def test_revalidate_evicts_events_stored_before_the_rules_tightened(tmp_path):
    state = State(tmp_path / "s.json")
    store = Store(tmp_path / "seen.jsonl", tmp_path / "db", state)
    junk = Event(title="Free International PUBCRAWL w/ Local Guides!",
                 organiser="Porto Internationals", url="https://a",
                 location="Baixa Bar, Porto", start_date=date(2026, 9, 1),
                 end_date=date(2026, 9, 1))
    good = Event(title="Porto Robotics Hackathon 2026", organiser="JuniFEUP",
                 url="https://b", type="hackathon", fields=["mechanical"],
                 location="FEUP, Porto", start_date=date(2026, 11, 7),
                 end_date=date(2026, 11, 8), duration_days=2, cost="free")
    for e in (junk, good):
        enrich_estimates(e, CFG)
        store.upsert(e, TODAY)
    assert len(store.open) == 2

    evicted = store.revalidate(
        lambda ev: filters.apply_all(ev, CFG, None, TODAY), TODAY)
    assert len(evicted) == 1 and "PUBCRAWL" not in str(list(store.open.values()))
    assert len(store.open) == 1
    # And it must not come back tomorrow.
    status, _ = store.upsert(junk, TODAY)
    assert status == "suppressed"


# ------------------------------------------------------------------- roundup
def test_roundup_rows_are_expandable_and_carry_the_full_card():
    e = Event(title="Some Workshop", organiser="Org", url="https://x",
              type="workshop", fields=["data_ai"], location="Porto",
              start_date=date(2026, 9, 3), end_date=date(2026, 9, 3),
              start_time="18:00", tz_known=True, cost="free")
    enrich_estimates(e, CFG)
    row = R._roundup_row(e, TODAY, "me@example.com")
    assert row.startswith("<tr><td><details>") and "<summary" in row
    for must in ("When", "Where", "Money", "Read more and apply",
                 "Interested", "Meh", "Not for me"):
        assert must in row, f"expanded row is missing {must}"


def test_scores_state_their_own_basis():
    e = Event(title="X", organiser="Bosch", url="https://x", type="hackathon")
    enrich_estimates(e, CFG)
    assert "known-names list" in e.estimate_notes["prestige"]
    assert "hackathon baseline" in e.estimate_notes["cv"]
    card = R._card_body(e, TODAY, "me@example.com")
    assert ">Why<" in card and "baseline" in card
