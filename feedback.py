"""Feedback via email reply, polled over IMAP at the start of each run.

Two commands, two different lessons:
  not-interested: E-0247, E-0301  -> suppress permanently AND downweight the
                                     type/organiser/field going forward
  cant-attend: E-0255             -> suppress this instance only; never touches
                                     any weight
"""
from __future__ import annotations

import email
import imaplib
import re
from datetime import date
from email.header import decode_header
from typing import Dict, List

from store import Store, State, normalise

CMD_RE = re.compile(r"^\s*(not[\s\-_]?interested|can'?t[\s\-_]?attend)\s*[:\-]\s*(.+)$",
                    re.IGNORECASE | re.MULTILINE)
ID_RE = re.compile(r"\bE[-\s]?(\d{3,5})\b", re.IGNORECASE)
REASONS = ["too expensive", "wrong field", "too basic", "too advanced",
           "bad timing", "low prestige", "dislike this format",
           "dislike this organiser"]


def parse_body(text: str) -> Dict[str, List[str]]:
    """Pure parser. Ignores quoted reply lines (>) and the footer's own
    syntax examples (E-0247/E-0301/E-0255 shown as documentation)."""
    lines = [ln for ln in (text or "").splitlines() if not ln.lstrip().startswith(">")]
    clean = "\n".join(ln for ln in lines
                      if "->" not in ln and "suppress" not in ln.lower())
    out = {"not_interested": [], "cant_attend": [], "reasons": []}
    for m in CMD_RE.finditer(clean):
        cmd, payload = m.group(1).lower(), m.group(2)
        ids = [f"E-{int(n):04d}" for n in ID_RE.findall(payload)]
        key = "not_interested" if cmd.startswith("not") else "cant_attend"
        out[key].extend(i for i in ids if i not in out[key])
    low = clean.lower()
    m = re.search(r"reason\s*[:\-]\s*(.+)", low)
    scope = m.group(1) if m else low
    for r in REASONS:
        if r in scope and r not in out["reasons"]:
            out["reasons"].append(r)
    return out


def apply_feedback(parsed: Dict[str, List[str]], store: Store, state: State,
                   cfg: dict, today: date) -> Dict[str, int]:
    stats = {"not_interested": 0, "cant_attend": 0, "reasons": len(parsed["reasons"])}
    dw = state.data["downweights"]
    for eid in parsed["not_interested"]:
        ev = store.find_by_id(eid)
        if not ev:
            continue
        for key in ([f"type:{ev.type}", f"organiser:{normalise(ev.organiser)}"]
                    + [f"field:{f}" for f in ev.fields]):
            dw[key] = dw.get(key, 0) + 1
        store.dismiss(ev, "not_interested", today)
        if eid not in state.data["ask_reason_for"]:
            state.data["ask_reason_for"].append(eid)
        stats["not_interested"] += 1
    for eid in parsed["cant_attend"]:
        ev = store.find_by_id(eid)
        if not ev:
            continue
        store.dismiss(ev, "cant_attend", today)   # weights untouched, by design
        stats["cant_attend"] += 1
    for r in parsed["reasons"]:
        state.data["feedback_reasons"].append(
            {"reason": r, "date": today.isoformat()})
        adj = (cfg["scoring"]["reason_adjustments"] or {}).get(r)
        if adj and adj.get("key"):
            dw[adj["key"]] = dw.get(adj["key"], 0) + 1
    if parsed["reasons"]:
        state.data["ask_reason_for"] = []          # question answered
    return stats


def _plain_part(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace")
                except Exception:
                    continue
        return ""
    payload = msg.get_payload(decode=True)
    return payload.decode(msg.get_content_charset() or "utf-8", "replace") \
        if payload else ""


def poll_inbox(store: Store, state: State, cfg: dict, today: date, *,
               gmail_address: str, app_password: str,
               diagnostics: dict) -> None:
    """Read unseen replies to prior digests. Any failure lands in diagnostics
    and never kills the run."""
    try:
        with imaplib.IMAP4_SSL("imap.gmail.com", 993) as imap:
            imap.login(gmail_address, app_password)
            imap.select("INBOX")
            _, data = imap.uid("search", None,
                               '(UNSEEN SUBJECT "Opportunity Scout")')
            uids = (data[0] or b"").split()
            for uid in uids:
                _, msgdata = imap.uid("fetch", uid, "(RFC822)")
                if not msgdata or not msgdata[0]:
                    continue
                msg = email.message_from_bytes(msgdata[0][1])
                parsed = parse_body(_plain_part(msg))
                stats = apply_feedback(parsed, store, state, cfg, today)
                diagnostics.setdefault("feedback", []).append(stats)
                imap.uid("store", uid, "+FLAGS", "(\\Seen)")
    except Exception as e:                          # noqa: BLE001
        diagnostics["feedback_error"] = f"{type(e).__name__}: {e}"[:200]
