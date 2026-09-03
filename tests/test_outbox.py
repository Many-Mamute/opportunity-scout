"""The prepare -> deliver handoff. A digest parked at 06:10 must arrive at
06:30 intact, and a missing or stale outbox must fail loudly rather than
quietly sending nothing."""
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

import main

CFG = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "filters.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def outbox(tmp_path, monkeypatch):
    box = tmp_path / "outbox.json"
    monkeypatch.setattr(main, "OUTBOX", box)
    return box


def _park(box, on: date, subject="Opportunity Scout — 3 new · 1 deadlines closing"):
    box.write_text(json.dumps({
        "prepared_at": datetime.now(ZoneInfo("Europe/Lisbon")).isoformat(),
        "date": on.isoformat(), "subject": subject,
        "html": "<body>digest</body>", "text": "digest",
        "recipient": "someone@example.com"}))


def test_deliver_sends_and_clears(outbox, monkeypatch):
    today = datetime.now(ZoneInfo("Europe/Lisbon")).date()
    _park(outbox, today)
    sent = {}
    monkeypatch.setattr(main.email_render, "send",
                        lambda s, h, t, **kw: sent.update(subject=s, **kw))
    assert main.deliver(CFG) == 0
    assert sent["subject"].startswith("Opportunity Scout")
    assert sent["recipient"] == "someone@example.com"
    assert not outbox.exists()          # cleared, so it can never send twice


def test_missing_outbox_fails_loudly(outbox, monkeypatch):
    monkeypatch.setattr(main.email_render, "send",
                        lambda *a, **k: pytest.fail("must not send"))
    assert main.deliver(CFG) == 1       # non-zero -> GitHub emails you


def test_stale_outbox_is_refused(outbox, monkeypatch):
    yesterday = datetime.now(ZoneInfo("Europe/Lisbon")).date() - timedelta(days=1)
    _park(outbox, yesterday)
    monkeypatch.setattr(main.email_render, "send",
                        lambda *a, **k: pytest.fail("must not send stale mail"))
    assert main.deliver(CFG) == 1
    assert outbox.exists()              # left in place for diagnosis


# ------------------------------------------------- Tavily budget accounting
def test_daily_resets_but_monthly_persists(tmp_path):
    from datetime import date as D
    from store import State
    s = State(tmp_path / "s.json")
    for _ in range(24):
        s.tavily_spend(1, D(2026, 8, 16))     # pin the day: default is today()
    assert s.tavily_remaining(D(2026, 8, 16), 24, 900) == 0
    # New day: daily allowance returns, monthly total keeps counting.
    assert s.tavily_remaining(D(2026, 8, 17), 24, 900) == 24
    assert s.data["tavily"]["month_used"] == 24


def test_monthly_cap_overrides_the_daily_allowance(tmp_path):
    from datetime import date as D
    from store import State
    s = State(tmp_path / "s.json")
    s.tavily_remaining(D(2026, 8, 16), 24, 900)
    s.data["tavily"]["month_used"] = 895
    assert s.tavily_remaining(D(2026, 8, 16), 24, 900) == 5   # not 24


def test_month_rollover_clears_the_monthly_counter(tmp_path):
    from datetime import date as D
    from store import State
    s = State(tmp_path / "s.json")
    s.tavily_remaining(D(2026, 8, 31), 24, 900)
    s.data["tavily"]["month_used"] = 900
    assert s.tavily_remaining(D(2026, 8, 31), 24, 900) == 0
    assert s.tavily_remaining(D(2026, 9, 1), 24, 900) == 24
