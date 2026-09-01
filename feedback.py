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
    r"^\s*(interested|meh|uninterested|not[\s\-_]?interested|can'?t[\s\-_]?attend"
    r"|shortlist|pin|unshortlist|unpin|applied|didn'?t[\s\-_]?apply|skipped)"
    r"\s*[:\-]\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_ALIAS = {"interested": "interested", "meh": "meh",
          "uninterested": "uninterested", "notinterested": "uninterested",
          "cantattend": "meh",
          # Pin something to the top of every digest until its deadline passes.
          "shortlist": "shortlist", "pin": "shortlist",
          "unshortlist": "unshortlist", "unpin": "unshortlist",
          # Close the loop: did the thing you liked actually get an application?
          "applied": "applied", "didntapply": "skipped", "skipped": "skipped"}
_VERDICTS = ("interested", "meh", "uninterested", "shortlist", "unshortlist",
             "applied", "skipped")
ID_RE = re.compile(r"\bE[-\s]?(\d{1,5})\b", re.IGNORECASE)
REASONS = ["too expensive", "wrong field", "too basic", "too advanced",
           "bad timing", "low prestige", "dislike this format",
           "dislike this organiser"]


# Fingerprints of the scout's own output. A length heuristic is not enough:
# a short digest containing the words "too expensive" would be read as a reason.
DIGEST_MARKERS = ("OPPORTUNITY SCOUT —", "Reply -> interested:",
                  "Tap a button on any card")


def parse_body(text: str) -> Dict[str, List[str]]:
    """Pure parser. Ignores quoted reply lines (>) and the footer's own
    syntax examples (E-0247/E-0301/E-0255 shown as documentation)."""
    if any(m in (text or "") for m in DIGEST_MARKERS):
        return {"interested": [], "meh": [], "uninterested": [],
                "reasons": [], "notes": []}
    lines = [ln for ln in (text or "").splitlines() if not ln.lstrip().startswith(">")]
    clean = "\n".join(ln for ln in lines
                      if "->" not in ln and "suppress" not in ln.lower())
    out = {k: [] for k in _VERDICTS}
    out.update({"reasons": [], "notes": []})
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
    # Free-text lines are justifications for a verdict. Without a verdict there
    # is nothing to justify, and the scout's own digest arrives in this same
    # inbox — without this guard every line of it became "feedback".
    has_command = any(out[k] for k in _VERDICTS)
    for ln in (clean.splitlines() if has_command else []):
        s = ln.strip()
        if (len(s) > 12 and not CMD_RE.match(ln) and not s.startswith("(")
                and "optional" not in s.lower() and s[:1].isalpha()):
            out["notes"].append(s[:300])
    low = clean.lower()
    # Not the digest (checked above), so a bare reason line is a genuine
    # follow-up answer to "why?".
    if True:
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

    for eid in parsed.get("shortlist", []):
        ev = store.find_by_id(eid)
        if ev:
            ev.pinned = True
            stats["shortlist"] = stats.get("shortlist", 0) + 1
    for eid in parsed.get("unshortlist", []):
        ev = store.find_by_id(eid)
        if ev:
            ev.pinned = False
            stats["unshortlist"] = stats.get("unshortlist", 0) + 1

    # Did the interest turn into an application? Recorded per type so the
    # ranking can eventually tell enthusiasm from follow-through.
    for outcome in ("applied", "skipped"):
        for eid in parsed.get(outcome, []):
            ev = store.find_by_id(eid)
            if not ev:
                continue
            ev.applied = "yes" if outcome == "applied" else "no"
            state.data.setdefault("outcomes", []).append(
                {"id": eid, "type": ev.type, "applied": ev.applied,
                 "date": today.isoformat()})
            stats[outcome] = stats.get(outcome, 0) + 1

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


def _ensure_folder(imap, folder: str) -> bool:
    try:
        imap.create(f'"{folder}"')                # already exists -> harmless NO
    except imaplib.IMAP4.error:
        pass
    typ, _ = imap.select(f'"{folder}"')
    return typ == "OK"


def _file_away(imap, uid, folder: str, diagnostics: dict) -> None:
    """Move a processed reply out of the inbox.

    Your feedback is machine input, not correspondence — once it has been read
    it should not sit in the inbox forever. COPY + \\Deleted + EXPUNGE is the
    IMAP idiom for a move; Gmail turns it into a label change.
    """
    try:
        typ, _ = imap.uid("copy", uid, f'"{folder}"')
        if typ == "OK":
            imap.uid("store", uid, "+FLAGS", "(\\Deleted)")
            imap.expunge()
    except imaplib.IMAP4.error as e:
        diagnostics.setdefault("feedback_notes", []).append(
            f"could not file reply away: {e}"[:120])


def poll_inbox(store: Store, state: State, cfg: dict, today: date, *,
               gmail_address: str, app_password: str,
               diagnostics: dict) -> None:
    """Read replies to prior digests, then move them out of the inbox.

    Both INBOX and the archive folder are checked: if you add a Gmail filter so
    replies never touch the inbox at all, they are still found. Any failure
    lands in diagnostics and never kills the run."""
    folder = cfg["settings"].get("feedback_folder", "Opportunity Scout")
    try:
        with imaplib.IMAP4_SSL("imap.gmail.com", 993) as imap:
            imap.login(gmail_address, app_password)
            _ensure_folder(imap, folder)          # create it if missing
            for mailbox in ("INBOX", f'"{folder}"'):
                _poll_one(imap, mailbox, folder, store, state, cfg, today,
                          diagnostics)
    except Exception as e:                          # noqa: BLE001
        diagnostics["feedback_error"] = f"{type(e).__name__}: {e}"[:200]


def _poll_one(imap, mailbox: str, folder: str, store: Store, state: State,
              cfg: dict, today: date, diagnostics: dict) -> None:
    in_inbox = mailbox.strip('"') == "INBOX"
    key = "last_imap_uid" if in_inbox else "last_imap_uid_folder"
    try:
        if imap.select(mailbox)[0] != "OK":
            return
        # Gmail auto-marks self-addressed replies as read, so UNSEEN is not a
        # reliable filter. Track the highest UID processed per mailbox instead.
        last = int(state.data.get(key, 0))
        _, data = imap.uid("search", None,
                           f'(UID {last + 1}:* SUBJECT "Opportunity Scout")')
        uids = [u for u in (data[0] or b"").split() if int(u) > last]
        for uid in uids:
            _, msgdata = imap.uid("fetch", uid, "(RFC822)")
            if not msgdata or not msgdata[0]:
                continue
            raw = msgdata[0][1] if isinstance(msgdata[0], tuple) else msgdata[0]
            msg = email.message_from_bytes(raw)
            # Advance the counter even for skipped mail, so the same message is
            # never reconsidered — and so a move that fails cannot loop forever.
            state.data[key] = max(int(state.data.get(key, 0)), int(uid))
            if msg.get("X-Opportunity-Scout"):
                continue                          # our own digest, not a reply
            parsed = parse_body(_plain_part(msg))
            stats = apply_feedback(parsed, store, state, cfg, today)
            diagnostics.setdefault("feedback", []).append(stats)
            imap.uid("store", uid, "+FLAGS", "(\\Seen)")
            if in_inbox:
                _file_away(imap, uid, folder, diagnostics)
    except Exception as e:                          # noqa: BLE001
        diagnostics["feedback_error"] = f"{type(e).__name__}: {e}"[:200]
