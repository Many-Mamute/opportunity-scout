"""Second-opinion pass, domestic twins, affiliation bonus, send timing."""
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

import enrich as E
import filters
import main
from models import NOT_STATED, Event
from score import enrich_estimates, score

CFG = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "filters.yaml").read_text(encoding="utf-8"))
filters.prime(CFG)
TODAY = date(2026, 8, 31)


# ------------------------------------------------------------ independence
def test_registrable_domain_collapses_subdomains():
    assert E.registrable("https://careers.bosch.pt/jobs/1") == "bosch.pt"
    assert E.registrable("https://www.bbc.co.uk/news") == "bbc.co.uk"
    assert E.registrable("https://noticias.publico.pt/a") == "publico.pt"


def test_a_source_must_be_genuinely_separate():
    ev = Event(title="Porto Robotics Hackathon", url="https://lu.ma/abc",
               one_line_summary="A two-day robotics hackathon for students in Porto.")
    results = [
        {"url": "https://lu.ma/other", "content": "...", "title": "same site"},
        {"url": "https://www.eventbrite.com/e/1", "content": "...", "title": "aggregator"},
        {"url": "https://linkedin.com/posts/x", "content": "...", "title": "social"},
        {"url": "https://jn.pt/n", "title": "syndicated",
         "content": "A two-day robotics hackathon for students in Porto."},
        {"url": "https://publico.pt/a", "title": "real reporting",
         "content": "Organisers expect 200 participants from six universities."},
        {"url": "https://desporto.publico.pt/b", "title": "same publisher",
         "content": "Another angle entirely on the same event."},
    ]
    kept = E.independent_results(ev, results)
    assert [s["domain"] for s in kept] == ["publico.pt"]


def test_gaps_are_only_claimed_where_they_exist():
    bare = Event(title="X", organiser="Unknown Org", url="https://x")
    enrich_estimates(bare, CFG)
    gaps = E.needs_enrichment(bare, CFG)
    assert {"dates", "cost", "organiser standing", "deadline"} <= set(gaps)

    complete = Event(title="Y", organiser="CERN", url="https://y", cost="free",
                     start_date=TODAY, end_date=TODAY, next_deadline=TODAY)
    enrich_estimates(complete, CFG)
    assert E.needs_enrichment(complete, CFG) == []


# --------------------------------------------------------- applying results
def _sources():
    return [{"domain": "publico.pt", "url": "https://publico.pt/a", "title": "t",
             "text": "..."},
            {"domain": "jn.pt", "url": "https://jn.pt/b", "title": "t", "text": "..."}]


def test_findings_fill_gaps_and_record_who_said_so():
    ev = Event(title="Porto Engineering Week", organiser="Some Association",
               url="https://x", type="conference")
    enrich_estimates(ev, CFG)
    before = ev.prestige_score
    filled = E.apply_findings(ev, {
        "about_this_event": True, "start_date": "2027-03-04",
        "end_date": "2027-03-06", "application_deadline": "2027-02-01",
        "cost": "free", "location": "Porto, Portugal",
        "organiser_description": "A national engineering students' association.",
        "reputation": "Participants describe it as the largest student "
                      "engineering event in the country.",
        "reputation_level": "national", "supported_by": [1, 2]}, _sources(), CFG)
    assert {"dates", "deadline", "cost", "prestige"} <= set(filled)
    assert ev.start_date == date(2027, 3, 4) and ev.duration_days == 3
    assert ev.cost == "free" and ev.next_deadline is None  # stage added, not set yet
    assert any(s.closes == date(2027, 2, 1) for s in ev.stages)
    assert ev.prestige_score == 3 > before
    assert ev.corroborated_by == ["jn.pt", "publico.pt"]
    assert "publico.pt" in ev.corroboration_note
    assert "checked against other sources" in ev.flags
    # And the score explanation stops blaming the lookup table.
    assert "independent coverage" in ev.estimate_notes["prestige"]


def test_a_mismatched_result_writes_nothing():
    ev = Event(title="Porto Engineering Week", organiser="Org", url="https://x")
    enrich_estimates(ev, CFG)
    filled = E.apply_findings(ev, {"about_this_event": False,
                                   "start_date": "2099-01-01", "cost": "free"},
                              _sources(), CFG)
    assert filled == [] and ev.start_date is None and ev.cost == NOT_STATED


def test_enrichment_never_overwrites_a_stated_fact():
    ev = Event(title="X", organiser="Org", url="https://x", cost="free",
               start_date=date(2027, 5, 1), end_date=date(2027, 5, 1))
    enrich_estimates(ev, CFG)
    E.apply_findings(ev, {"about_this_event": True, "start_date": "2099-01-01",
                          "cost": "paid (€900)", "reputation_level": "local"},
                     _sources(), CFG)
    assert ev.start_date == date(2027, 5, 1) and ev.cost == "free"


# ------------------------------------------------------------ domestic twins
def test_foreign_edition_is_flagged_when_a_portuguese_one_is_open():
    pt = Event(title="Formula Student Portugal 2027", id="E-0001",
               url="https://a", location="Vila Real, Portugal")
    de = Event(title="Formula Student Germany 2027", id="E-0002",
               url="https://b", location="Hockenheim, Germany")
    solo = Event(title="Shell Eco-marathon Europe 2027", id="E-0003",
                 url="https://c", location="Nogaro, France")
    assert E.mark_domestic_twins([pt, de, solo], CFG) == 1
    assert de.domestic_twin == "E-0001" and pt.domestic_twin is None
    assert solo.domestic_twin is None            # no Portuguese edition exists
    assert "a Portuguese edition exists: E-0001" in de.flags


def test_twin_detection_needs_a_real_title_not_noise():
    a = Event(title="Portugal 2027", url="https://a", location="Porto")
    b = Event(title="Germany 2027", url="https://b", location="Berlin")
    assert E.mark_domestic_twins([a, b], CFG) == 0


# -------------------------------------------------------- affiliation bonus
def test_own_faculty_outranks_an_identical_outside_event():
    mine = Event(title="Robotics workshop", organiser="NEEMec — FEUP",
                 url="https://neemec.pt/x", type="workshop",
                 fields=["mechanical"], source="feup-nucleos")
    theirs = Event(title="Robotics workshop", organiser="Random Org",
                   url="https://x.pt", type="workshop", fields=["mechanical"],
                   source="meetup-porto")
    for e in (mine, theirs):
        enrich_estimates(e, CFG)
        score(e, CFG, TODAY, {})
    assert mine.score > theirs.score
    assert "your own faculty" in mine.flags and "your own faculty" not in theirs.flags


# ------------------------------------------------------- new interest areas
def test_public_speaking_and_finance_are_recognised():
    cases = [
        ("Toastmasters Porto — open evening", "public_speaking"),
        ("Workshop de oratória e comunicação em público", "public_speaking"),
        ("Model United Nations Porto 2027", "public_speaking"),
        ("Sessão de literacia financeira para estudantes", "economics_literacy"),
        ("Introduction to personal finance and investing basics", "economics_literacy"),
    ]
    for title, want in cases:
        ev = Event(title=title, url="https://x", organiser="Org")
        filters.classify(ev, CFG)
        assert want in ev.fields, f"{title!r} -> {ev.fields}"


def test_adventure_is_not_entrepreneurship():
    ev = Event(title="Walk + Kayak Adventure at Ponte Luís I", url="https://x",
               organiser="Porto walks")
    filters.classify(ev, CFG)
    assert "entrepreneurship" not in ev.fields


# ------------------------------------------------------------- send timing
def test_deliver_waits_until_the_exact_send_time(monkeypatch):
    slept = {}
    monkeypatch.setattr(main.time, "sleep", lambda s: slept.setdefault("s", s))
    tz = ZoneInfo(CFG["settings"]["timezone"])
    fake_now = datetime.now(tz).replace(hour=5, minute=50, second=0, microsecond=0)

    class FakeDT(datetime):
        @classmethod
        def now(cls, tzinfo=None):
            return fake_now
    monkeypatch.setattr(main, "datetime", FakeDT)
    main._wait_until_send_time(CFG)
    assert slept["s"] == 600                     # 05:50 -> 06:00 is ten minutes


def test_deliver_sends_immediately_if_github_was_late(monkeypatch):
    slept = {}
    monkeypatch.setattr(main.time, "sleep", lambda s: slept.setdefault("s", s))
    tz = ZoneInfo(CFG["settings"]["timezone"])
    late = datetime.now(tz).replace(hour=6, minute=7, second=0, microsecond=0)

    class FakeDT(datetime):
        @classmethod
        def now(cls, tzinfo=None):
            return late
    monkeypatch.setattr(main, "datetime", FakeDT)
    main._wait_until_send_time(CFG)
    assert "s" not in slept                      # already past 06:00, send now


def test_wait_is_capped(monkeypatch):
    slept = {}
    monkeypatch.setattr(main.time, "sleep", lambda s: slept.setdefault("s", s))
    tz = ZoneInfo(CFG["settings"]["timezone"])
    early = datetime.now(tz).replace(hour=1, minute=0, second=0, microsecond=0)

    class FakeDT(datetime):
        @classmethod
        def now(cls, tzinfo=None):
            return early
    monkeypatch.setattr(main, "datetime", FakeDT)
    main._wait_until_send_time(CFG)
    assert slept["s"] == CFG["settings"]["max_send_wait_min"] * 60
