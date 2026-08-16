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


def test_online_event_at_night_is_flagged_and_kept():
    """The Guatemala workshop: 19:00 UTC-6 is 02:00 here. Keep it, but make
    the hour impossible to overlook."""
    e = Event(title="Cursor Guatemala Workshop", url="https://x", format="online",
              location="online", start_date=date(2026, 8, 21),
              start_time="02:00", tz_known=True, tz_source="UTC-06:00")
    ok, _ = filters._online_hours(e)
    assert ok
    assert any("02:00 Lisbon" in f for f in e.flags), e.flags
    enrich_estimates(e, CFG)
    card = R._card_body(e, TODAY, "me@example.com")
    assert "02:00 Lisbon" in card and "up at night" in card


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
    assert "prestige table scores" in e.estimate_notes["prestige"]
    assert "starts at 4 on the event-type table" in e.estimate_notes["cv"]
    card = R._card_body(e, TODAY, "me@example.com")
    assert "How scored" in card and "event-type table" in card
    # Full sentences, not shorthand.
    assert "est. —" not in card


# ------------------------------------------------------- Sunday full recap
def _open_set():
    mk = lambda t, s, conf="verified": Event(
        title=t, organiser="Org", url=f"https://{t}", type="hackathon",
        fields=["mechanical"], location="Porto", confidence=conf,
        start_date=date(2026, 9, 1), end_date=date(2026, 9, 1), score=s)
    return [mk("Alpha", 0.9), mk("Bravo", 0.8), mk("Charlie", 0.4),
            mk("Delta snippet", 0.3, "low")]


def test_sunday_recap_lists_everything_including_the_cards():
    evs = _open_set()
    for e in evs:
        enrich_estimates(e, CFG)
    top, low = evs[:2], [evs[3]]
    _, html, text = R.build(
        date(2026, 8, 16), True, act_now=[], top=top, worth_travel=[],
        roundup=evs,                      # Sunday: the complete inventory
        low_conf=low, diagnostics={}, ask_reason_for=[], reply_to="x@y.z")
    assert "Sunday recap — everything still open" in html
    assert "all 4 opportunities you have not rejected" in html
    for e in evs:                         # every open item appears in the recap
        assert e.title in text
    assert "SUNDAY RECAP" in text


def test_weekday_list_is_only_the_overflow():
    evs = _open_set()
    for e in evs:
        enrich_estimates(e, CFG)
    _, html, text = R.build(
        date(2026, 8, 18), False, act_now=[], top=evs[:2], worth_travel=[],
        roundup=evs[2:], low_conf=[], diagnostics={}, ask_reason_for=[],
        reply_to="x@y.z")
    assert "Also still open" in html and "Sunday recap" not in html
    assert "2 more — tap any line" in html


# ------------------------------------------- truncated snippet text (run 2)
def test_snippet_starting_mid_sentence_is_repaired():
    """Verbatim from E-0047 in the 16 Aug digest."""
    raw = ('uts motorsports on February 25, 2026: "This is your chance to join '
           'a Formula Student team while at uni! Applications are now open at the')
    out = R.clean_text(raw)
    assert out.startswith("This is your chance")
    assert "uts motorsports" not in out


def test_leading_fragment_marked_when_unrecoverable():
    out = R.clean_text("reamble with nothing clean to cut back to anywhere here")
    assert out.startswith("…")


def test_midsentence_start_jumps_to_next_sentence():
    out = R.clean_text("ing the deadline. Applications are now open for 2027. "
                       "Teams of four compete over two days.")
    assert out.startswith("Applications are now open")


def test_trailing_ellipsis_normalised_and_quotes_balanced():
    assert R.clean_text("Join a Formula Student team while at uni ...").endswith("…")
    assert R.clean_text('"An unbalanced opening quote here').count('"') == 0


def test_good_text_is_left_alone():
    s = "A weekend of creating solutions that put users back in control."
    assert R.clean_text(s) == s


def test_build_stamp_appears_in_diagnostics():
    from models import BUILD
    _, html, text = R.build(date(2026, 8, 18), False, act_now=[], top=[],
                            worth_travel=[], roundup=[], low_conf=[],
                            diagnostics={"runtime_s": 12}, ask_reason_for=[],
                            reply_to="x@y.z")
    assert f"Build {BUILD}" in html and f"Build {BUILD}" in text



# ---------------------------------------- card layout fixes from run 3
def test_roundup_body_does_not_repeat_the_title():
    """<summary> stays visible when open, so the body must not repeat it."""
    e = Event(title="CERN Summer Student Programme 2027", organiser="CERN",
              url="https://home.cern", type="internship", fields=["mechanical"],
              location="Geneva, Switzerland")
    enrich_estimates(e, CFG)
    row = R._roundup_row(e, TODAY, "me@example.com")
    body = row.split("</summary>", 1)[1]
    assert "CERN Summer Student Programme 2027" not in body
    assert "When" in body and "Interested" in body      # facts still there
    assert row.count("CERN Summer Student Programme 2027") == 1


def test_requirements_are_not_truncated():
    long_req = ("Undergraduate students are eligible if they have completed at "
                "least six semesters of full-time studies in physics, "
                "engineering, computer science or mathematics by the start of "
                "the programme, and are enrolled at the time of application.")
    e = Event(title="X", organiser="CERN", url="https://x", type="internship",
              prerequisites=long_req)
    enrich_estimates(e, CFG)
    card = R._card_body(e, TODAY, "me@example.com")
    assert "enrolled at the time of application" in card
    assert "six semesters of…" not in card


def test_grammar_of_the_rationale():
    for typ in ("internship", "workshop", "other", "hackathon"):
        e = Event(title="X", organiser="Nobody", url="https://x", type=typ)
        enrich_estimates(e, CFG)
        blob = " ".join(e.estimate_notes.values())
        assert " a internship" not in blob and " a other" not in blob
