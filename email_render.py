"""Digest rendering + Gmail SMTP send.

Layout principles, after the first live run came back unreadable:
  - one fact per labelled row, never a dot-separated run-on line;
  - the three things you decide on (when/where, money, link) are large; the
    estimates are small and footnoted once, not re-labelled four times;
  - markdown and emoji from scraped descriptions are stripped, not passed through;
  - every card ends with three one-tap feedback links.
"""
from __future__ import annotations

import re
import smtplib
import ssl
from datetime import date
from email.message import EmailMessage
from html import escape
from typing import List, Optional, Tuple
from urllib.parse import quote

from models import BUILD, NOT_STATED, Event, Stage

INK, MUT, LINE, ACCENT, WARM, BG = (
    "#22252b", "#6f7580", "#e6e2d9", "#14453a", "#8a5a1f", "#faf9f6")

FOOTER_SYNTAX = (
    "Tap a button on any card, or reply with these lines:\n"
    "  interested: E-0004      -> more of this kind, please\n"
    "  meh: E-0004             -> not this one, but keep looking in this area\n"
    "  uninterested: E-0004    -> wrong direction, stop showing these\n"
    "Add a sentence after any line saying why — it is read and used.\n"
    "Several IDs per line are fine: interested: E-0004, E-0011"
)


def subject_line(n_new: int, m_closing: int, sunday: bool) -> str:
    s = f"Opportunity Scout — {n_new} new · {m_closing} deadlines closing"
    return s + (" · weekly roundup" if sunday else "")


# ------------------------------------------------------------------ cleaning
_MD = [(r"```.*?```", " "), (r"`([^`]*)`", r"\1"), (r"\*\*([^*]*)\*\*", r"\1"),
       (r"\*([^*]*)\*", r"\1"), (r"_{2,}([^_]*)_{2,}", r"\1"),
       (r"^#{1,6}\s*", ""), (r"\[([^\]]*)\]\([^)]*\)", r"\1"),
       (r"https?://\S+", " "), (r"[#*_>`~]+", " ")]
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200d]+")


# Search snippets are cut arbitrarily at both ends by the provider, so they
# routinely open mid-word: 'uts motorsports on February 25, 2026: "This is your
# chance...'. Recover a clean start rather than printing the wreckage.
_ATTRIB = re.compile(r'^.{0,120}?\bon\s+\w+\s+\d{1,2},?\s+\d{4}\s*[:\-]\s*["\u201c]?')
_QUOTE_LEAD = re.compile(r'^.{0,120}?[:\u2014-]\s*["\u201c]')
_SENTENCE = re.compile(r'(?<=[.!?])\s+(?=[A-Z"\u201c])')


def _repair_lead(t: str) -> str:
    """Trim a truncated opening fragment; mark it if nothing clean is found."""
    if not t:
        return t
    for pat in (_ATTRIB, _QUOTE_LEAD):
        m = pat.match(t)
        if m and len(t) - m.end() > 40:
            return t[m.end():].lstrip()
    # Starts mid-word or mid-sentence: jump to the next real sentence if one
    # begins soon enough, otherwise say plainly that the start is missing.
    if t[0].islower():
        m = _SENTENCE.search(t[:200])
        if m and len(t) - m.end() > 40:
            return t[m.end():]
        return "…" + t
    return t


def clean_text(raw: str, limit: int = 260) -> str:
    """Scraped copy arrives full of markdown bold, headers and emoji. Strip it
    all — the digest has its own typography."""
    t = raw or ""
    for pat, rep in _MD:
        t = re.sub(pat, rep, t, flags=re.DOTALL | re.MULTILINE)
    t = _EMOJI.sub("", t)
    t = re.sub(r"\s+", " ", t).strip(" -–—·|")
    t = re.sub(r"\s*\.{3,}\s*$", "…", t)          # trailing " ..." -> "…"
    t = _repair_lead(t)
    if len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    # An unbalanced quote left by the trim reads as an error.
    if t.count('"') % 2:
        t = t.replace('"', "", 1) if t.startswith('"') else t.rstrip('"')
    return t.strip()


def _d(day: date) -> str:
    return f"{day.strftime('%a')} {day.day} {day.strftime('%b')}"


def _when(ev: Event, today: date) -> str:
    if not ev.start_date:
        return "Dates not stated"
    multi = ev.end_date and ev.end_date != ev.start_date
    s = _d(ev.start_date)
    if ev.start_time and not multi:
        s += f", {ev.start_time}"
        if ev.end_time and ev.end_time != ev.start_time:
            s += f"–{ev.end_time}"
    if multi:
        if ev.start_time:
            s += f", {ev.start_time}"
        s += f" → {_d(ev.end_date)}"
        if ev.end_time:
            s += f", {ev.end_time}"
        s += f" ({(ev.end_date - ev.start_date).days + 1} days)"
    # Times are converted, so say whose clock this is — the whole point is that
    # you never have to work it out.
    if ev.start_time:
        s += (" Lisbon time" if ev.tz_known
              else " (time as published, no timezone stated)")
        if ev.tz_known and ev.tz_source and ev.tz_source not in ("UTC+00:00",
                                                                 "UTC+01:00"):
            s += f", published {ev.tz_source}"
    elif ev.format == "online":
        s += " · start time not stated"
    delta = (ev.start_date - today).days
    when = ("today" if delta == 0 else "tomorrow" if delta == 1
            else f"in {delta} days" if 0 < delta < 90 else "")
    return f"{s} · {when}" if when else s


def _where(ev: Event) -> str:
    if ev.format == "online":
        return "Online"
    place = ev.location if ev.location != NOT_STATED else "location not stated"
    mins = ev.travel_time_from_porto_min
    if mins == 0:
        return f"In person · {place}"
    if mins:
        return f"In person · {place} · about {mins} min from Porto"
    return f"In person · {place}"


def _money(ev: Event) -> str:
    """Cost and earnings on one line, because that is one decision."""
    bits = []
    if ev.cost == "free":
        bits.append("Free")
    elif ev.cost == "funded":
        bits.append("Funded — they cover the costs")
    elif ev.cost != NOT_STATED:
        # cost arrives as "paid (€15)"; strip the wrapper rather than printing it
        pretty = ev.cost.replace("paid ", "").strip()
        if pretty.startswith("(") and pretty.endswith(")"):
            pretty = pretty[1:-1]
        bits.append(f"Costs {pretty}")
    else:
        bits.append("Cost not stated")
    if ev.paid_unpaid and ev.paid_unpaid not in (NOT_STATED, ""):
        bits.append(f"pays {ev.paid_unpaid}")
    if ev.rewards and ev.rewards not in (NOT_STATED, "none stated", ""):
        bits.append(f"you get {ev.rewards.replace('_', ' ')}")
    return " · ".join(bits)


def _extras(ev: Event) -> List[Tuple[str, str]]:
    out = []
    if ev.team_requirement != NOT_STATED:
        out.append(("Team", ev.team_requirement.replace("_", " ")))
    if ev.selectivity != NOT_STATED:
        out.append(("Entry", ev.selectivity.replace("_", " ")))
    if ev.prerequisites != NOT_STATED:
        out.append(("Requires", clean_text(ev.prerequisites, 2000)))
    if ev.age_limits != NOT_STATED:
        out.append(("Age", ev.age_limits))
    if ev.value_justification:
        out.append(("Worth it?", clean_text(ev.value_justification, 2000)))
    if ev.language != NOT_STATED:
        out.append(("Language", ev.language))
    if ev.organiser_note:
        out.append(("Who they are", clean_text(ev.organiser_note, 300)))
    if ev.corroboration_note:
        out.append(("Cross-checked", ev.corroboration_note))
    if ev.domestic_twin:
        out.append(("Note", f"a Portuguese edition of this is also open "
                            f"({ev.domestic_twin}) — compare before travelling"))
    for s in ev.stages:
        span = " ".join(x for x in [
            f"opens {s.opens.day} {s.opens.strftime('%b')}" if s.opens else "",
            f"closes {s.closes.day} {s.closes.strftime('%b')}" if s.closes else ""
        ] if x)
        label = "Apply early" if s.is_advantageous else "Stage"
        out.append((label, f"{s.name} — {span}" if span else s.name))
    return out


# ---------------------------------------------------------------- components
def _pill(text: str, strong: bool = False) -> str:
    bg, fg = (ACCENT, "#ffffff") if strong else ("#f2f0ea", INK)
    return (f'<span style="display:inline-block;background:{bg};color:{fg};'
            f'border-radius:11px;padding:3px 10px;font:600 11px/1.4 '
            f'-apple-system,Segoe UI,sans-serif;margin:0 5px 5px 0">'
            f'{escape(text)}</span>')


def _warn_pill(text: str) -> str:
    return (f'<span style="display:inline-block;background:#fdf3e3;color:{WARM};'
            f'border:1px solid #e8d5b0;border-radius:11px;padding:3px 10px;'
            f'font:600 11px/1.4 -apple-system,Segoe UI,sans-serif;'
            f'margin:0 5px 5px 0">{escape(text)}</span>')


def _row(label: str, value: str) -> str:
    return (f'<tr><td style="padding:3px 12px 3px 0;color:{MUT};'
            f'font:600 11px/1.6 -apple-system,Segoe UI,sans-serif;'
            f'text-transform:uppercase;letter-spacing:.05em;white-space:nowrap;'
            f'vertical-align:top">{escape(label)}</td>'
            f'<td style="padding:3px 0;color:{INK};font:400 13px/1.6 Georgia,serif">'
            f'{escape(value)}</td></tr>')


def _row_multi(label: str, lines: List[str]) -> str:
    body = "<br>".join(escape(x) for x in lines)
    return (f'<tr><td style="padding:3px 12px 3px 0;color:{MUT};'
            f'font:600 11px/1.6 -apple-system,Segoe UI,sans-serif;'
            f'text-transform:uppercase;letter-spacing:.05em;white-space:nowrap;'
            f'vertical-align:top">{escape(label)}</td>'
            f'<td style="padding:3px 0;color:{MUT};font:400 12px/1.7 Georgia,serif">'
            f'{body}</td></tr>')


def _feedback(ev: Event, reply_to: str) -> str:
    def link(cmd: str, text: str, colour: str) -> str:
        body = quote(f"{cmd}: {ev.id}\n\n(optional, one line on why:)\n")
        href = (f"mailto:{reply_to}?subject={quote('Opportunity Scout feedback')}"
                f"&body={body}")
        return (f'<a href="{href}" style="display:inline-block;border:1px solid '
                f'{colour};color:{colour};border-radius:14px;padding:6px 15px;'
                f'margin:0 7px 0 0;font:600 12px -apple-system,Segoe UI,sans-serif;'
                f'text-decoration:none">{text}</a>')
    return ('<div style="margin-top:14px">'
            + link("interested", "Interested", ACCENT)
            + link("meh", "Meh", WARM)
            + link("uninterested", "Not for me", MUT)
            + '</div>')


def _card_body(ev: Event, today: date, reply_to: str, lead: str = "",
               show_header: bool = True) -> str:
    head = ""
    if lead:
        head = (f'<div style="background:#fdf3e3;color:{WARM};font:700 11px '
                f'-apple-system,Segoe UI,sans-serif;letter-spacing:.05em;'
                f'text-transform:uppercase;padding:5px 11px;border-radius:4px;'
                f'display:inline-block;margin-bottom:10px">{escape(lead)}</div>')

    title = (f'<div style="font:700 18px/1.35 Georgia,serif;margin-bottom:3px">'
             f'<a href="{escape(ev.url)}" style="color:{INK};text-decoration:none">'
             f'{escape(clean_text(ev.title, 110))}</a></div>')
    org_name = ev.organiser if ev.organiser != NOT_STATED else "Organiser not stated"
    org = (f'<div style="color:{MUT};font:400 13px Georgia,serif;margin-bottom:10px">'
           f'{escape(org_name)} &nbsp;·&nbsp; '
           f'<span style="font:600 11px monospace">{ev.id}</span></div>')

    pills = (_pill("Online" if ev.format == "online" else "In person", True)
             + _pill(ev.type.replace("_", " ").title())
             + "".join(_pill(f.replace("_", " ")) for f in ev.fields[:3]))
    if "new today" in ev.flags:
        pills = _pill("New today", True) + pills
    # Things you should see before deciding: awkward hours, term clashes,
    # travel exceptions, age advantages, and any change since last time.
    for f in dict.fromkeys(ev.flags):
        if f == "new today":
            continue
        if (f.startswith("starts ") or f.startswith("changed: ")
                or f in ("worth the travel", "clashes with term time",
                         "age-category advantage", "you marked this interesting")):
            pills += _warn_pill(f)
    if ev.calendar_conflict in ("near_exams", "clashes_with_term_time") \
            and "clashes with term time" not in ev.flags:
        pills += _warn_pill("clashes with term time")

    summary = clean_text(ev.one_line_summary) if ev.one_line_summary else ""
    summary_html = (f'<div style="font:400 14px/1.65 Georgia,serif;color:{INK};'
                    f'margin:2px 0 13px">{escape(summary)}</div>' if summary else "")

    rows = [_row("When", _when(ev, today)), _row("Where", _where(ev))]
    if ev.next_deadline:
        d = ev.days_until_next_deadline
        rows.append(_row("Deadline", _d(ev.next_deadline)
                         + (f" — {d} days left" if d is not None else "")))
    rows.append(_row("Money", _money(ev)))
    rows += [_row(k, v) for k, v in _extras(ev)]
    rows.append(_row("Effort", f"about {ev.estimated_effort_hours}h "
                               f"({ev.intensity})*"))
    rows.append(_row("Scored", f"CV {ev.cv_value_score}/5 · prestige "
                               f"{ev.prestige_score}/5 · networking "
                               f"{ev.networking_score}/5*"))
    basis = [ev.estimate_notes[k] for k in ("cv", "prestige", "networking", "effort")
             if ev.estimate_notes.get(k)]
    if basis:
        rows.append(_row_multi("How scored", basis))
    table = ('<table role="presentation" cellpadding="0" cellspacing="0" '
             'style="width:100%;border-collapse:collapse">'
             + "".join(rows) + "</table>")

    note = ""
    if ev.analysis:
        body = " ".join(escape(clean_text(x, 600)) for x in ev.analysis)
        skills = (f'<div style="margin-top:6px;color:{MUT};font-size:12px">'
                  f'<b>You would come away with:</b> '
                  f'{escape(", ".join(ev.skills_developed))}.</div>'
                  if ev.skills_developed else "")
        note = (f'<div style="margin-top:12px;padding:11px 13px;'
                f'background:#f6f4ee;border-left:3px solid {ACCENT};'
                f'font:400 13px/1.6 Georgia,serif;color:{INK}">'
                f'<div style="font:700 11px -apple-system,Segoe UI,sans-serif;'
                f'letter-spacing:.07em;text-transform:uppercase;color:{ACCENT};'
                f'margin-bottom:5px">Why this one</div>{body}{skills}</div>')

    link = (f'<div style="margin-top:12px"><a href="{escape(ev.url)}" '
            f'style="color:{ACCENT};font:600 13px -apple-system,Segoe UI,sans-serif;'
            f'text-decoration:none">Read more and apply →</a>'
            f'<span style="color:{MUT};font:400 11px -apple-system,sans-serif">'
            f'&nbsp; via {escape(ev.source)}'
            + ("&nbsp;· unverified snippet" if ev.confidence == "low" else "")
            + '</span></div>')

    if not show_header:
        # The <summary> line above already carries the title and organiser and
        # stays on screen while the row is open.
        title = org = ""
    return (f'{head}{title}{org}<div style="margin-bottom:9px">{pills}</div>'
            f'{summary_html}{table}{note}{link}{_feedback(ev, reply_to)}')


def _card(ev: Event, today: date, reply_to: str, lead: str = "") -> str:
    return (f'<tr><td style="padding:22px;border-bottom:1px solid {LINE}">'
            + _card_body(ev, today, reply_to, lead) + '</td></tr>')


def _compact_row(ev: Event, today: date, reply_to: str,
                 already_shown: bool = False) -> str:
    """One scannable row, complete enough to decide on without opening a link.

    This used to be a <details> toggle. Gmail strips the tag but keeps the
    contents, so every row rendered fully expanded — which on a Sunday meant
    the whole digest printed twice, and the ＋ marker appeared on some titles
    and not others. A toggle that only works in half the world's mail clients
    is worse than no toggle, so: compact rows, and no duplicate bodies for
    events that already have a full card higher up.
    """
    head = (f'<a href="{escape(ev.url)}" style="color:{INK};font:700 14px '
            f'Georgia,serif;text-decoration:none">'
            f'{escape(clean_text(ev.title, 78))}</a>'
            f'<span style="color:{MUT};font:400 13px Georgia,serif"> — '
            f'{escape(ev.organiser)}</span>')

    if already_shown:
        return (f'<tr><td style="padding:8px 22px;border-bottom:1px dotted {LINE}">'
                f'{head}<span style="color:{ACCENT};font:600 11px '
                f'-apple-system,Segoe UI,sans-serif"> · full card above</span>'
                f'</td></tr>')

    facts = " · ".join(x for x in [
        _when(ev, today), _where(ev), _money(ev),
        (f"deadline {_d(ev.next_deadline)}" if ev.next_deadline
         else "no stated deadline")] if x)
    desc = clean_text(ev.one_line_summary, 170) if ev.one_line_summary else ""
    desc_html = (f'<div style="color:{INK};font:400 13px/1.55 Georgia,serif;'
                 f'margin-top:4px">{escape(desc)}</div>' if desc else "")
    warn = [f for f in dict.fromkeys(ev.flags)
            if f.startswith("starts ") or f in ("worth the travel",
                                                "clashes with term time",
                                                "age-category advantage")]
    warn_html = ("".join(_warn_pill(w) for w in warn) if warn else "")
    return (f'<tr><td style="padding:13px 22px;border-bottom:1px solid {LINE}">'
            f'{head}'
            f'<div style="color:{MUT};font:400 12px/1.6 Georgia,serif;'
            f'margin-top:3px">{escape(facts)}</div>'
            f'{desc_html}'
            + (f'<div style="margin-top:6px">{warn_html}</div>' if warn else "")
            + f'<div style="margin-top:7px">'
              f'<a href="{escape(ev.url)}" style="color:{ACCENT};font:600 12px '
              f'-apple-system,Segoe UI,sans-serif;text-decoration:none">'
              f'Read more →</a></div>'
            + _feedback(ev, reply_to)
            + '</td></tr>')


def _section(title: str, inner: str, note: str = "") -> str:
    if not inner:
        return ""
    n = (f'<div style="color:{MUT};font:400 12px/1.5 Georgia,serif;margin-top:4px">'
         f'{escape(note)}</div>' if note else "")
    return (f'<tr><td style="padding:26px 22px 11px;background:#f6f4ee;'
            f'border-bottom:1px solid {LINE}">'
            f'<div style="font:700 12px -apple-system,Segoe UI,sans-serif;'
            f'letter-spacing:.11em;text-transform:uppercase;color:{ACCENT}">'
            f'{escape(title)}</div>{n}</td></tr>{inner}')


# --------------------------------------------------------------------- build
def build(today: date, sunday: bool, *,
          act_now: List[Tuple[Event, Stage, str]],
          top: List[Event],
          worth_travel: List[Event],
          roundup: List[Event],
          low_conf: List[Event],
          diagnostics: dict,
          ask_reason_for: List[str],
          reply_to: str = "",
          pinned: Optional[List[Event]] = None,
          not_yet: Optional[List[Event]] = None,
          ask_applied: Optional[List[Event]] = None,
          expected_soon: Optional[List[str]] = None) -> Tuple[str, str, str]:
    pinned = pinned or []
    not_yet = not_yet or []
    ask_applied = ask_applied or []
    expected_soon = expected_soon or []
    not_yet = not_yet or []
    closing = {e.id for e in roundup + top + worth_travel
               if e.days_until_next_deadline is not None
               and e.days_until_next_deadline <= 7}
    closing |= {ev.id for ev, _, kind in act_now if kind in ("t7", "t2")}

    act_leads, act_events, seen = {}, [], set()
    for ev, st, kind in act_now:
        label = {"opens": f"applications open: {st.name}",
                 "t7": f"7 days left — {st.name}",
                 "t2": f"2 days left — {st.name}"}[kind]
        if ev.id in seen:
            act_leads[ev.id] += f" · {label}"
        else:
            seen.add(ev.id)
            act_events.append(ev)
            act_leads[ev.id] = label
    for e in top:
        if e.id not in seen and e.days_until_next_deadline is not None \
                and e.days_until_next_deadline <= 7:
            seen.add(e.id)
            act_events.append(e)
            act_leads[e.id] = f"deadline in {e.days_until_next_deadline} days"
    top = [e for e in top if e.id not in seen]
    subject = subject_line(len(top) + len(act_events) + len(worth_travel),
                           len(closing), sunday)

    parts = []
    if ask_reason_for:
        parts.append(
            f'<tr><td style="padding:16px 22px;background:#fdf3e3;'
            f'font:400 13px/1.6 Georgia,serif;color:{INK}">'
            f'<b>One question.</b> You passed on '
            f'{escape(", ".join(ask_reason_for))} — a sentence on why lets the '
            f'ranking learn the right lesson instead of guessing. Just reply.'
            f'</td></tr>')
    if ask_applied:
        rows = "".join(
            f'<li style="margin-bottom:4px"><b>{e.id}</b> '
            f'{escape(clean_text(e.title, 70))} — deadline '
            + (f"{_d(e.next_deadline)}" if e.next_deadline else "now")
            + "</li>" for e in ask_applied)
        parts.append(
            f'<tr><td style="padding:16px 22px;background:#fdf3e3;'
            f'font:400 13px/1.6 Georgia,serif;color:{INK}">'
            f'<b>Did you actually apply?</b> You marked these interesting and '
            f'their deadline has arrived:<ul style="margin:7px 0 7px 18px;'
            f'padding:0">{rows}</ul>'
            f'Reply <code>applied: {ask_applied[0].id}</code> or '
            f'<code>skipped: {ask_applied[0].id}</code> — it is asked once, and '
            f'it is how the scout learns which enthusiasm turns into действия.'
            f'</td></tr>'.replace("действия", "an application"))
    parts.append(_section("Your shortlist", "".join(
        _card(e, today, reply_to, "pinned by you") for e in pinned),
        "pinned until the deadline passes — reply `unpin: ID` to drop one"))
    parts.append(_section("Act now", "".join(
        _card(e, today, reply_to, act_leads.get(e.id, "")) for e in act_events),
        "stages opening and deadlines within a week"))
    parts.append(_section(f"Today's picks ({len(top)})", "".join(
        _card(e, today, reply_to) for e in top)))
    parts.append(_section("Worth the travel", "".join(
        _card(e, today, reply_to) for e in worth_travel),
        "funded or exceptionally prestigious, so the 1-hour rule is waived"))
    if roundup:
        carded = {e.id for e in act_events + top + worth_travel + low_conf
                  if e.id}
        inner = "".join(_compact_row(e, today, reply_to,
                                     bool(e.id) and e.id in carded)
                        for e in roundup)
        week = [e for e in roundup if e.days_until_next_deadline is not None
                and e.days_until_next_deadline <= 7]
        closing_note = (f"{len(week)} close within 7 days" if week
                        else "none close within 7 days")
        if sunday:
            heading = "Sunday recap — everything still open"
            dupes = sum(1 for e in roundup if e.id and e.id in carded)
            note = (f"all {len(roundup)} opportunities you have not rejected, "
                    f"soonest deadline first; {closing_note}"
                    + (f". {dupes} of them "
                       + ("has" if dupes == 1 else "have")
                       + " a full card above and appears here as one line only"
                       if dupes else ""))
        else:
            heading = "Also still open"
            note = (f"{len(roundup)} more that did not make the top ten; "
                    f"{closing_note}")
        parts.append(_section(heading, inner, note))
    if expected_soon:
        rows = "".join(
            f'<tr><td style="padding:9px 22px;border-bottom:1px dotted {LINE};'
            f'font:400 13px/1.55 Georgia,serif;color:{INK}">{escape(x)}</td>'
            f'</tr>' for x in expected_soon)
        parts.append(_section("Expected to reopen soon", rows,
                              "from dates the scout saw in previous years — "
                              "predictions, not listings, so verify before "
                              "relying on them"))
    if not_yet:
        rows = "".join(
            f'<tr><td style="padding:9px 22px;border-bottom:1px dotted {LINE};'
            f'font:400 13px/1.5 Georgia,serif">'
            f'<a href="{escape(e.url)}" style="color:{INK}">'
            f'{escape(clean_text(e.title, 74))}</a>'
            f'<span style="color:{MUT}"> — {escape(e.organiser)} · '
            f'you qualify from <b>{_d(e.eligible_from)} '
            f'{e.eligible_from.year}</b></span></td></tr>'
            for e in not_yet if e.eligible_from)
        parts.append(_section("Not yet — but worth knowing about", rows,
                              "you do not have enough semesters behind you for "
                              "these; they return on their own once you do"))

    parts.append(_section("Unverified", "".join(
        _card(e, today, reply_to) for e in low_conf),
        "found only as a search snippet — check the link before acting"))

    diag_lines = [
        f"{diagnostics.get('sources_queried', 0)} sources checked, "
        f"{diagnostics.get('fetched', 0)} pages fetched, "
        f"{diagnostics.get('unchanged', 0)} unchanged since yesterday",
        "Failed: " + (", ".join(
            f"{k} ({v})" for k, v in diagnostics.get("sources_errored", {}).items())
            or "none"),
        f"Tavily {diagnostics.get('tavily_used', 0)} used today, "
        f"{diagnostics.get('tavily_remaining', 0)} left"
        + (f" · {diagnostics['tavily_month']}/900 this month"
           if diagnostics.get("tavily_month") is not None else "")
        + (" — cap hit, sweep skipped" if diagnostics.get("tavily_exhausted") else "")
        + (f" — {diagnostics['tavily_note']}" if diagnostics.get("tavily_note") else ""),
        f"Gemini {diagnostics.get('gemini_calls', 0)} calls"
        + (f" on {diagnostics['gemini_model_used']}"
           if diagnostics.get("gemini_model_used") else "")
        + (f", {len(diagnostics.get('llm_dropped', []))} pages dropped at the cap"
           if diagnostics.get("llm_dropped") else ""),
        f"Store: {diagnostics.get('open_total', 0)} open, "
        f"{diagnostics.get('deferred_total', 0)} waiting on eligibility, "
        f"{diagnostics.get('dismissed_total', 0)} rejected or dismissed"
        + (f", {diagnostics['evicted_count']} retired by today's filters"
           if diagnostics.get("evicted_count") else ""),
        ("Quiet week: " + diagnostics["fallback"]
         if diagnostics.get("fallback") else None),
        ("Now eligible: " + ", ".join(diagnostics["released"])
         if diagnostics.get("released") else None),
        "Cross-checks: " + str(diagnostics.get("enrichment", "not run"))
        + (f" · {diagnostics['domestic_twins']} foreign event(s) have a "
           f"Portuguese twin" if diagnostics.get("domestic_twins") else ""),
        "Filtered out: " + (", ".join(
            f"{v}× {k}" for k, v in sorted(diagnostics.get("rejected", {}).items(),
                                           key=lambda x: -x[1])[:6]) or "nothing"),
        f"Build {BUILD} · runtime {diagnostics.get('runtime_s', 0)}s"
        + (" — HARD STOP reached, partial results"
           if diagnostics.get("timed_out") else ""),
    ]
    diag_lines = [d for d in diag_lines if d]
    if diagnostics.get("feedback_error"):
        diag_lines.append(f"Could not read replies: {diagnostics['feedback_error']}")
    if diagnostics.get("gemini_errors"):
        diag_lines.append("Gemini: " + "; ".join(diagnostics["gemini_errors"][:2]))

    parts.append(_section("Diagnostics",
                          f'<tr><td style="padding:12px 22px;color:{MUT};'
                          f'font:400 12px/1.7 -apple-system,Segoe UI,sans-serif">'
                          + "<br>".join(escape(x) for x in diag_lines)
                          + "</td></tr>"))

    html = (f'<body style="margin:0;padding:0;background:{BG}">'
            f'<table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" style="background:{BG}"><tr><td align="center">'
            f'<table role="presentation" width="640" style="max-width:640px;'
            f'width:100%;background:#fff;border:1px solid {LINE}" '
            f'cellpadding="0" cellspacing="0">'
            f'<tr><td style="padding:22px;border-bottom:3px solid {ACCENT}">'
            f'<div style="font:700 21px Georgia,serif;color:{INK}">'
            f'Opportunity Scout</div>'
            f'<div style="color:{MUT};font:400 13px Georgia,serif;margin-top:2px">'
            f'{escape(today.strftime("%A") + f" {today.day} " + today.strftime("%B %Y"))}'
            f'{" · weekly roundup" if sunday else ""}</div></td></tr>'
            + "".join(parts) +
            f'<tr><td style="padding:18px 22px;color:{MUT};font:400 11px/1.7 '
            f'-apple-system,Segoe UI,sans-serif;border-top:1px solid {LINE};'
            f'white-space:pre-line">'
            f'* effort and the 1-5 scores are the scout&#39;s estimates from lookup '
            f'tables in config/filters.yaml, not facts from the page and not '
            f'judgements about this specific event. CV starts from the event '
            f'type and gains +1 for a recognised organiser; prestige is purely '
            f'organiser-name matching; networking is the event type alone. '
            f'Correct them by replying — that is what trains the ranking. '
            f'Everything else is quoted from the source or says '
            f'&quot;not stated&quot;.\n\n{escape(FOOTER_SYNTAX)}</td></tr>'
            f'</table></td></tr></table></body>')

    text = _plain_text(today, sunday, act_events, act_leads, top, worth_travel,
                       roundup, low_conf, diag_lines, ask_reason_for,
                       pinned=pinned, ask_applied=ask_applied,
                       not_yet=not_yet,
                       expected_soon=expected_soon)
    return subject, html, text


def _plain_text(today, sunday, act_events, act_leads, top, worth_travel,
                roundup, low_conf, diag_lines, ask_reason_for,
                pinned=None, ask_applied=None, expected_soon=None,
                not_yet=None) -> str:
    pinned = pinned or []
    not_yet = not_yet or []
    ask_applied = ask_applied or []
    expected_soon = expected_soon or []
    not_yet = not_yet or []
    def block(e: Event, lead="") -> str:
        lines = [f"{e.id}  {clean_text(e.title, 90)}"]
        if lead:
            lines.append(f"  ** {lead} **")
        lines.append(f"  {e.organiser} · {e.type.replace('_', ' ')}"
                     + (f" · {'/'.join(e.fields)}" if e.fields else ""))
        if e.one_line_summary:
            lines.append(f"  {clean_text(e.one_line_summary, 200)}")
        lines += [f"  When:  {_when(e, today)}", f"  Where: {_where(e)}"]
        notable = [f for f in dict.fromkeys(e.flags)
                   if f.startswith("starts ") or f.startswith("changed: ")
                   or f in ("worth the travel", "clashes with term time",
                            "age-category advantage", "new today")]
        if notable:
            lines.append("  Note:  " + " | ".join(notable))
        if e.next_deadline:
            lines.append(f"  Deadline: {e.next_deadline} "
                         f"({e.days_until_next_deadline} days left)")
        lines.append(f"  Money: {_money(e)}")
        lines += [f"  {k}: {v}" for k, v in _extras(e)]
        lines.append(f"  Scored: CV {e.cv_value_score}/5 · prestige "
                     f"{e.prestige_score}/5 · networking {e.networking_score}/5*")
        for k in ("cv", "prestige", "networking", "effort"):
            if e.estimate_notes.get(k):
                lines.append(f"    - {e.estimate_notes[k]}")
        if e.analysis:
            lines.append("  Why this one:")
            for sentence in e.analysis:
                lines.append(f"    {clean_text(sentence, 600)}")
        if e.skills_developed:
            lines.append("  Builds: " + ", ".join(e.skills_developed))
        lines += [f"  Link:  {e.url}",
                  f"  Reply -> interested: {e.id} | meh: {e.id} "
                  f"| uninterested: {e.id}"]
        return "\n".join(lines)

    out = [f"OPPORTUNITY SCOUT — {today.strftime('%A')} {today.day} "
           f"{today.strftime('%B %Y')}", ""]
    if ask_reason_for:
        out += [f"One question: you passed on {', '.join(ask_reason_for)} — "
                f"a sentence on why helps the ranking learn.", ""]
    if ask_applied:
        out += ["— DID YOU APPLY? " + "—" * 34, ""]
        out += [f"  {e.id}  {clean_text(e.title, 70)} — reply "
                f"'applied: {e.id}' or 'skipped: {e.id}'" for e in ask_applied]
        out += [""]
    if expected_soon:
        out += ["— EXPECTED TO OPEN SOON (predicted, not listed) " + "—" * 3, ""]
        out += [f"  {line}" for line in expected_soon] + [""]
    for name, group, leads in (("YOUR SHORTLIST", pinned, {}),
                               ("ACT NOW", act_events, act_leads),
                               ("TODAY'S PICKS", top, {}),
                               ("WORTH THE TRAVEL", worth_travel, {}),
                               ("UNVERIFIED (snippet only)", low_conf, {})):
        if group:
            out += [f"— {name} " + "—" * max(0, 48 - len(name)), ""]
            out += [block(e, leads.get(e.id, "")) + "\n" for e in group]
    if roundup:
        # Plain text has no toggle, so give each line enough to decide on
        # without opening the link.
        out += [("— SUNDAY RECAP: EVERYTHING STILL OPEN " + "—" * 13) if sunday
                else ("— ALSO STILL OPEN " + "—" * 33), ""]
        carded_t = {e.id for e in act_events + top + worth_travel + low_conf
                    if e.id}
        for e in roundup:
            if e.id and e.id in carded_t:
                out += [f"{e.id}  {clean_text(e.title, 70)} "
                        f"— {e.organiser} (full details above)"]
                continue
            out += [f"{e.id}  {clean_text(e.title, 70)}",
                    f"  {e.organiser} · {_when(e, today)} · {_where(e)}",
                    f"  {_money(e)} · "
                    + (f"deadline {e.next_deadline}" if e.next_deadline
                       else "no deadline stated"),
                    f"  {e.url}",
                    f"  Reply -> interested: {e.id} | meh: {e.id} "
                    f"| uninterested: {e.id}", ""]
        out += [""]
    if not_yet:
        out += ["— NOT YET, BUT WORTH KNOWING " + "—" * 21, ""]
        out += [f"  {e.id}  {clean_text(e.title, 66)} — {e.organiser} — "
                f"you qualify from {e.eligible_from}"
                for e in not_yet if e.eligible_from] + [""]
    out += ["— DIAGNOSTICS " + "—" * 37, ""] + diag_lines + [
        "", "* effort and the 1-5 scores are estimates, not facts from the page.",
        "", FOOTER_SYNTAX]
    return "\n".join(out)


# ---------------------------------------------------------------------- send
def send(subject: str, html: str, text: str, *, gmail_address: str,
         app_password: str, recipient: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = recipient
    msg["Reply-To"] = gmail_address
    # Lets the IMAP poller recognise and skip its own output, which lands in
    # the same inbox when you send the digest to yourself.
    msg["X-Opportunity-Scout"] = "digest"
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(gmail_address, app_password)
        s.send_message(msg)
