from datetime import date
from pathlib import Path

import yaml

from feedback import apply_feedback, parse_body
from models import Event
from store import State, Store

CFG = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "filters.yaml").read_text(encoding="utf-8"))
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


def test_the_digest_itself_is_not_read_as_feedback():
    """The digest lands in the same inbox the poller reads."""
    digest = ("OPPORTUNITY SCOUT — Sunday 16 August 2026\n"
              "E-0049  CERN Summer Student Programme 2027\n"
              "  CERN · internship · mechanical\n"
              "  The CERN Summer Student Programme 2027 is a flagship "
              "international programme for university students.\n"
              "  Where: In person · Geneva, Switzerland\n"
              "  Money: Cost not stated · too expensive to ignore\n")
    p = parse_body(digest)
    assert p["interested"] == [] and p["meh"] == [] and p["uninterested"] == []
    assert p["notes"] == [] and p["reasons"] == []


def test_a_real_reply_still_captures_its_justification():
    p = parse_body("interested: E-0049\nExactly the kind of thing I want.")
    assert p["interested"] == ["E-0049"]
    assert p["notes"] == ["Exactly the kind of thing I want."]


def test_short_followup_reason_without_an_id_still_counts():
    assert parse_body("It was too expensive for me.")["reasons"] == ["too expensive"]


# ------------------------------------ replies must leave the inbox (run 4)
class _FakeIMAP:
    """Enough IMAP to prove the move happens, without a network."""
    def __init__(self, messages):
        self.messages = messages          # {mailbox: {uid: raw_bytes}}
        self.selected = None
        self.created, self.copied, self.flagged, self.expunged = [], [], [], 0

    def login(self, *a):
        return ("OK", [])

    def create(self, folder):
        self.created.append(folder.strip('"'))
        return ("OK", [])

    def select(self, mailbox):
        name = mailbox.strip('"')
        self.selected = name
        return ("OK", []) if name in self.messages else ("NO", [])

    def uid(self, cmd, *args):
        box = self.messages.get(self.selected, {})
        if cmd == "search":
            return ("OK", [b" ".join(str(u).encode() for u in sorted(box))])
        if cmd == "fetch":
            uid = int(args[0])
            return ("OK", [(b"", box[uid])]) if uid in box else ("OK", [None])
        if cmd == "copy":
            self.copied.append((int(args[0]), args[1].strip('"')))
            return ("OK", [])
        if cmd == "store":
            self.flagged.append((int(args[0]), args[2]))
            return ("OK", [])
        return ("OK", [])

    def expunge(self):
        self.expunged += 1
        return ("OK", [])


def _raw(subject, body, scout_header=False):
    hdr = "X-Opportunity-Scout: digest\r\n" if scout_header else ""
    return (f"Subject: {subject}\r\n{hdr}Content-Type: text/plain\r\n\r\n"
            f"{body}").encode()


def test_processed_replies_are_moved_out_of_the_inbox(tmp_path, monkeypatch):
    import feedback as F
    state = State(tmp_path / "s.json")
    store = Store(tmp_path / "seen.jsonl", tmp_path / "db", state)
    ev = Event(title="CERN Summer Student Programme", organiser="CERN",
               url="https://home.cern", type="internship", fields=["mechanical"])
    store.upsert(ev, TODAY)

    imap = _FakeIMAP({
        "INBOX": {7: _raw("Opportunity Scout feedback",
                          f"interested: {ev.id}\nGreat fit for me.")},
        "Opportunity Scout": {},
    })
    monkeypatch.setattr(F.imaplib, "IMAP4_SSL",
                        lambda *a, **k: _ctx(imap))
    diag = {}
    F.poll_inbox(store, state, CFG, TODAY, gmail_address="a@b.c",
                 app_password="x", diagnostics=diag)

    assert "Opportunity Scout" in imap.created          # folder ensured
    assert (7, "Opportunity Scout") in imap.copied      # copied across
    assert (7, "(\\Deleted)") in imap.flagged           # and removed from INBOX
    assert imap.expunged >= 1
    assert state.data["downweights"]["type:internship"] == -1   # and applied
    assert "feedback_error" not in diag


def test_the_digest_is_never_moved_or_processed(tmp_path, monkeypatch):
    import feedback as F
    state = State(tmp_path / "s.json")
    store = Store(tmp_path / "seen.jsonl", tmp_path / "db", state)
    imap = _FakeIMAP({
        "INBOX": {3: _raw("Opportunity Scout — 4 new",
                          "OPPORTUNITY SCOUT — Sunday\ninterested: E-0004 -> more",
                          scout_header=True)},
        "Opportunity Scout": {},
    })
    monkeypatch.setattr(F.imaplib, "IMAP4_SSL", lambda *a, **k: _ctx(imap))
    F.poll_inbox(store, state, CFG, TODAY, gmail_address="a@b.c",
                 app_password="x", diagnostics={})
    assert imap.copied == [] and imap.expunged == 0
    assert state.data["last_imap_uid"] == 3        # seen, skipped, not repeated


class _ctx:
    """Minimal context-manager wrapper around the fake."""
    def __init__(self, obj):
        self.obj = obj

    def __enter__(self):
        return self.obj

    def __exit__(self, *a):
        return False
