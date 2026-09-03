from datetime import date
from pathlib import Path

from extract import parse_jsonld, snippet_event

FIX = Path(__file__).parent / "fixtures"


def test_jsonld_full_mapping():
    html = (FIX / "eventbrite_event.html").read_text(encoding="utf-8")
    evs = parse_jsonld(html, "https://www.eventbrite.com/e/x", "eventbrite-porto", 200)
    assert len(evs) == 1
    e = evs[0]
    assert e.title == "Porto Robotics Hackathon 2026"
    assert e.organiser == "JuniFEUP"
    assert e.start_date == date(2026, 11, 7) and e.end_date == date(2026, 11, 8)
    assert e.duration_days == 2 and e.format == "in_person"
    assert "Porto" in e.location and e.cost == "free"
    assert e.confidence == "verified" and e.http_status == 200
    assert e.stages[0].name == "Registration closes"
    assert e.stages[0].closes == date(2026, 11, 1)
    assert e.url.startswith("https://www.eventbrite.com/e/porto-robotics")


def test_jsonld_graph_and_online():
    html = (FIX / "graph_online_event.html").read_text(encoding="utf-8")
    evs = parse_jsonld(html, "https://x", "src", 200)
    assert len(evs) == 1
    e = evs[0]
    assert e.format == "online" and e.organiser == "QuantAcademy"
    assert e.cost == "paid (€250)"


def test_no_jsonld_yields_nothing():
    html = (FIX / "no_jsonld.html").read_text(encoding="utf-8")
    assert parse_jsonld(html, "https://x", "src", 200) == []


def test_snippet_event_is_low_confidence_only_stated_facts():
    e = snippet_event("Bosch Braga summer internship", "https://linkedin.com/posts/x",
                      "We are hiring mechanical engineering interns.", "tavily-linkedin")
    assert e.confidence == "low"
    assert e.cost == "not stated" and e.start_date is None
