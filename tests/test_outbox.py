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

CFG = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "filters.yaml").read_text())


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
