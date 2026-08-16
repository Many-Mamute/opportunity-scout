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

# Three verdicts, three different lessons. "meh" is the interesting one: it
# suppresses the instance but keeps (slightly raises) the weight on the area,
# so the scout keeps hunting there instead of concluding you dislike the field.
CMD_RE = re.compile(
    r"^\s*(interested|meh|uninterested|not[\s\-_]?interested|can'?t[\s\-_]?attend)"
    r"\s*[:\-]\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_ALIAS = {"interested": "interested", "meh": "meh",
          "uninterested": "uninterested", "notinterested": "uninterested",
          "cantattend": "meh"}
ID_RE = re.compile(r"\bE[-\s]?(\d{1,5})\b", re.IGNORECASE)
REASONS = ["too expensive", "wrong field", "too basic", "too advanced",
           "bad timing", "low prestige", "dislike this format",
           "dislike this organiser"]


def parse_body(text: str) -> Dict[str, List[str]]:
    """Pure parser. Ignores quoted reply lines (>) and the footer's own
    syntax examples (E-0247/E-0301/E-0255 shown as documentation)."""
    lines = [ln for ln in (text or "").splitlines() if not ln.lstrip().startswith(">")]
    clean = "\n".join(ln for ln in lines
                      if "->" not in ln and "suppress" not in ln.lower())
    out = {"interested": [], "meh": [], "uninterested": [],
           "reasons": [], "notes": []}
    for m in CMD_RE.finditer(clean):
        raw = re.sub(r"[\s\-_\']", "", m.group(1).lower())
        verdict = _ALIAS.get(raw)
        if not verdict:
            continue
        payload = m.group(2)
        ids = [f"E-{int(n):04d}" for n in ID_RE.findall(payload)]
        out[verdict].extend(i for i in ids if i not in out[verdict])
        # Anything after the IDs on the same line is a free-text justification.
        tail = ID_RE.sub("", payload).strip(" ,.;:-")
        if len(tail) > 3:
            out["notes"].append(tail[:300])
    # Free-text lines that are not commands are treated as justification too.
    for ln in clean.splitlines():
        s = ln.strip()
        if (len(s) > 12 and not CMD_RE.match(ln) and not s.startswith("(")
                and "optional" not in s.lower() and s[:1].isalpha()):
            out["notes"].append(s[:300])
    low = clean.lower()
    for r in REASONS:
        if r in low and r not in out["reasons"]:
            out["reasons"].append(r)
    return out


def apply_feedback(parsed, store: Store, state: State,
                   cfg: dict, today: date) -> Dict[str, int]:
    """interested   -> boost this type/organiser/field, keep the event live
       meh          -> hide this instance, keep hunting in the same area
       uninterested -> hide forever and downweight the area"""
    stats = {k: 0 for k in ("interested", "meh", "uninterested")}
    dw = state.data["downweights"]

    def keys(ev) -> list:
        return ([f"type:{ev.type}", f"organiser:{normalise(ev.organiser)}"]
                + [f"field:{f}" for f in ev.fields])

    for eid in parsed.get("interested", []):
        ev = store.find_by_id(eid)
        if not ev:
            continue
        for k in keys(ev):
            dw[k] = dw.get(k, 0) - 1          # negative count = upweight
        ev.flags.append("you marked this interesting")
        stats["interested"] += 1

    for eid in parsed.get("meh", []):
        ev = store.find_by_id(eid)
        if not ev:
            continue
        # Only the field survives the shrug: keep searching the area, but stop
        # offering this organiser's version of it.
        for f in ev.fields:
            dw[f"field:{f}"] = min(0, dw.get(f"field:{f}", 0))
        store.dismiss(ev, "meh", today)
        stats["meh"] += 1

    for eid in parsed.get("uninterested", []):
        ev = store.find_by_id(eid)
        if not ev:
            continue
        for k in keys(ev):
            dw[k] = dw.get(k, 0) + 1
        store.dismiss(ev, "uninterested", today)
        if eid not in state.data["ask_reason_for"]:
            state.data["ask_reason_for"].append(eid)
        stats["uninterested"] += 1

    for r in parsed.get("reasons", []):
        state.data["feedback_reasons"].append({"reason": r,
                                               "date": today.isoformat()})
        adj = (cfg["scoring"].get("reason_adjustments") or {}).get(r)
        if adj and adj.get("key"):
            dw[adj["key"]] = dw.get(adj["key"], 0) + 1
    for note in parsed.get("notes", []):
        state.data["feedback_reasons"].append({"note": note,
                                               "date": today.isoformat()})
    if parsed.get("reasons") or parsed.get("notes"):
        state.data["ask_reason_for"] = []      # question answered
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
            # Gmail auto-marks self-addressed replies as read, so UNSEEN is not
            # a reliable filter. Track the highest UID processed instead.
            last = int(state.data.get("last_imap_uid", 0))
            _, data = imap.uid("search", None,
                               f"(UID {last + 1}:* SUBJECT \"Opportunity Scout\")")
            uids = [u for u in (data[0] or b"").split() if int(u) > last]
            for uid in uids:
                _, msgdata = imap.uid("fetch", uid, "(RFC822)")
                if not msgdata or not msgdata[0]:
                    continue
                msg = email.message_from_bytes(msgdata[0][1])
                parsed = parse_body(_plain_part(msg))
                stats = apply_feedback(parsed, store, state, cfg, today)
                diagnostics.setdefault("feedback", []).append(stats)
                state.data["last_imap_uid"] = max(
                    int(state.data.get("last_imap_uid", 0)), int(uid))
                imap.uid("store", uid, "+FLAGS", "(\\Seen)")
    except Exception as e:                          # noqa: BLE001
        diagnostics["feedback_error"] = f"{type(e).__name__}: {e}"[:200]
