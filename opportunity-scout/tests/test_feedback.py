from datetime import date
from pathlib import Path

import yaml

from feedback import apply_feedback, parse_body
from models import Event
from store import State, Store

CFG = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "filters.yaml").read_text())
TODAY = date(2026, 8, 13)


def test_three_verdicts_and_case():
    p = parse_body("Interested: E-0004\nMEH: E-0011, e-12\nuninterested: E-0002\n")
    assert p["interested"] == ["E-0004"]
    assert p["meh"] == ["E-0011", "E-0012"]
    assert p["uninterested"] == ["E-0002"]


def test_old_commands_still_work():
    """Digests already in the inbox use the old wording — don't break them."""
    p = parse_body("not-interested: E-0247\ncant-attend: E-0255")
    assert p["uninterested"] == ["E-0247"]
    assert p["meh"] == ["E-0255"]          # can't attend != wrong field


def test_free_text_justification_captured():
    p = parse_body("meh: E-0011 good field but the travel kills it")
    assert p["meh"] == ["E-0011"]
    assert "travel" in p["notes"][0]
    p2 = parse_body("uninterested: E-0003\nToo basic for me, I want harder things.")
    assert "too basic" in p2["reasons"]
    assert any("harder things" in n for n in p2["notes"])


def test_quoted_reply_and_footer_examples_ignored():
    body = ("meh: E-0009\n"
            "> uninterested: E-0001\n"                    # quoted digest text
            "  uninterested: E-0247  -> suppress forever AND downweight\n")
    p = parse_body(body)
    assert p["uninterested"] == []
    assert p["meh"] == ["E-0009"]


def _seed(tmp_path):
    state = State(tmp_path / "state.json")
    store = Store(tmp_path / "seen.jsonl", tmp_path / "db", state)
    made = []
    for title in ("Hack A", "Hack B", "Hack C"):
        e = Event(title=title, organiser="OrgX", url=f"https://{title}",
                  type="hackathon", fields=["mechanical"])
        store.upsert(e, TODAY)
        made.append(e.id)
    return state, store, made


def test_interested_upweights_and_keeps_event(tmp_path):
    state, store, (a, _, _) = _seed(tmp_path)
    apply_feedback(parse_body(f"interested: {a}"), store, state, CFG, TODAY)
    dw = state.data["downweights"]
    assert dw["type:hackathon"] == -1 and dw["field:mechanical"] == -1
    assert store.find_by_id(a) is not None          # still live, not dismissed


def test_meh_hides_instance_but_protects_the_field(tmp_path):
    state, store, (_, b, _) = _seed(tmp_path)
    state.data["downweights"]["field:mechanical"] = -2   # previously liked
    apply_feedback(parse_body(f"meh: {b}"), store, state, CFG, TODAY)
    dw = state.data["downweights"]
    assert store.find_by_id(b) is None               # this one is gone
    assert dw["field:mechanical"] == -2              # area still favoured
    assert "type:hackathon" not in dw                # type never penalised


def test_uninterested_downweights_everything_and_asks_why(tmp_path):
    state, store, (_, _, c) = _seed(tmp_path)
    apply_feedback(parse_body(f"uninterested: {c}"), store, state, CFG, TODAY)
    dw = state.data["downweights"]
    assert dw["type:hackathon"] == 1 and dw["organiser:orgx"] == 1
    assert store.find_by_id(c) is None
    assert state.data["ask_reason_for"] == [c]
    apply_feedback(parse_body("Wrong field, I want mechanical not software."),
                   store, state, CFG, TODAY)
    assert state.data["ask_reason_for"] == []        # answered, prompt cleared
    assert state.data["feedback_reasons"]
