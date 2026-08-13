from datetime import date

from feedback import apply_feedback, parse_body
from models import Event
from store import State, Store
import yaml
from pathlib import Path

CFG = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "filters.yaml").read_text())
TODAY = date(2026, 8, 13)


def test_parse_both_commands_and_case():
    body = ("Thanks!\n"
            "Not-Interested: E-0247, e-301\n"
            "cant attend: E-0255\n")
    p = parse_body(body)
    assert p["not_interested"] == ["E-0247", "E-0301"]
    assert p["cant_attend"] == ["E-0255"]


def test_quoted_reply_and_footer_examples_ignored():
    body = ("cant-attend: E-0009\n"
            "> not-interested: E-0001\n"                       # quoted digest
            "  not-interested: E-0247, E-0301   -> suppress forever AND downweight\n")
    p = parse_body(body)
    assert p["not_interested"] == []
    assert p["cant_attend"] == ["E-0009"]


def test_reason_parsing():
    p = parse_body("not-interested: E-0012\nreason: too expensive, wrong field\n")
    assert p["reasons"] == ["too expensive", "wrong field"]
    assert parse_body("just too basic for me")["reasons"] == ["too basic"]


def test_apply_semantics(tmp_path):
    state = State(tmp_path / "state.json")
    store = Store(tmp_path / "seen.jsonl", tmp_path / "db", state)
    a = Event(title="Hack A", organiser="OrgX", url="https://a",
              type="hackathon", fields=["data_ai"])
    b = Event(title="Hack B", organiser="OrgX", url="https://b",
              type="hackathon", fields=["data_ai"])
    store.upsert(a, TODAY)
    store.upsert(b, TODAY)
    ida, idb = a.id, b.id

    apply_feedback(parse_body(f"not-interested: {ida}"), store, state, CFG, TODAY)
    dw = state.data["downweights"]
    assert dw["type:hackathon"] == 1 and dw["organiser:orgx"] == 1 \
        and dw["field:data_ai"] == 1
    assert state.data["ask_reason_for"] == [ida]

    apply_feedback(parse_body(f"cant-attend: {idb}"), store, state, CFG, TODAY)
    # cant-attend must NEVER reduce the weight of a type or organiser
    assert dw["type:hackathon"] == 1 and dw["organiser:orgx"] == 1
    assert store.open == {}                      # both instances suppressed

    apply_feedback(parse_body("reason: dislike this format"), store, state, CFG, TODAY)
    assert state.data["ask_reason_for"] == []    # question answered, prompt cleared
    assert state.data["feedback_reasons"][0]["reason"] == "dislike this format"
