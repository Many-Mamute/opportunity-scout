"""Digest rendering + Gmail SMTP send.

Design: single-column table layout, inline styles (email clients ignore
stylesheets), one accent only — the event ID badge — so the ID the user
replies with is the most visible thing on every card. Plain-text mirror
carries identical information.
"""
from __future__ import annotations

import smtplib
import ssl
from datetime import date
from email.message import EmailMessage
from html import escape
from typing import Dict, List, Optional, Tuple

from models import Event, Stage

INK, MUT, LINE, BADGE, BG = "#1f2430", "#6b7280", "#e3e0d8", "#123c33", "#faf9f6"

FOOTER_SYNTAX = (
    "Reply to teach the scout. Two commands, two different lessons:\n"
    "  not-interested: E-0247, E-0301   -> suppress forever AND downweight this "
    "type/organiser/field\n"
    "  cant-attend: E-0255              -> suppress this one only; the type keeps "
    "full weight\n"
    "After a not-interested, add one line with a reason: too expensive / wrong "
    "field / too basic / too advanced / bad timing / low prestige / dislike this "
    "format / dislike this organiser."
)


def subject_line(n_new: int, m_closing: int, sunday: bool) -> str:
    s = f"Opportunity Scout — {n_new} new · {m_closing} deadlines closing"
    return s + (" · weekly roundup" if sunday else "")


# ------------------------------------------------------------------- pieces
def _est(ev: Event, field: str, value) -> str:
    note = ev.estimate_notes.get(field)
    return f"{value} ({note})" if note else str(value)


def _meta(ev: Event) -> str:
    bits = [ev.type.replace("_", " "),
            "/".join(ev.fields) if ev.fields else None,
            ev.format.replace("_", " "), ev.location,
            f"{ev.travel_time_from_porto_min} min from Porto"
            if ev.travel_time_from_porto_min is not None else None,
            f"{ev.start_date} → {ev.end_date}" if ev.start_date else "dates not stated",
            f"lang: {ev.language}"]
    return " · ".join(escape(str(b)) for b in bits if b)


def _stage_lines(ev: Event) -> List[str]:
    out = []
    for s in ev.stages:
        star = "★ advantageous — apply early" if s.is_advantageous else ""
        span = " ".join(x for x in [
            f"opens {s.opens}" if s.opens else "",
            f"closes {s.closes}" if s.closes else ""] if x)
        out.append(" — ".join(x for x in [s.name, span, s.notes, star] if x))
    return out


def _card_html(ev: Event, lead: str = "") -> str:
    rows = []
    if lead:
        rows.append(f'<div style="color:{BADGE};font-weight:600;font-size:13px;'
                    f'margin-bottom:4px">{escape(lead)}</div>')
    rows.append(
        f'<span style="background:{BADGE};color:#fff;border-radius:3px;'
        f'padding:2px 7px;font:600 12px monospace">{ev.id}</span> '
        f'<a href="{escape(ev.url)}" style="color:{INK};font-size:16px;'
        f'font-weight:700;text-decoration:none">{escape(ev.title)}</a>')
    rows.append(f'<div style="color:{MUT};font-size:12px;margin:3px 0">'
                f'{escape(ev.organiser)} · {_meta(ev)}</div>')
    if ev.one_line_summary:
        rows.append(f'<div style="font-size:13px">{escape(ev.one_line_summary)}</div>')
    dl = (f"deadline {ev.next_deadline} ({ev.days_until_next_deadline}d)"
          if ev.next_deadline else "deadline not stated")
    detail = [dl, f"cost: {ev.cost}"]
    if ev.value_justification:
        detail.append(ev.value_justification)
    if ev.paid_unpaid:
        detail.append(f"internship pay: {ev.paid_unpaid}")
    detail += [f"team: {ev.team_requirement}", f"selectivity: {ev.selectivity}",
               f"rewards: {ev.rewards}",
               f"effort ≈ {_est(ev, 'effort', ev.estimated_effort_hours)}h "
               f"[{ev.intensity}]",
               f"CV {_est(ev, 'cv', ev.cv_value_score)}/5 · "
               f"prestige {_est(ev, 'prestige', ev.prestige_score)}/5 · "
               f"networking {_est(ev, 'networking', ev.networking_score)}/5"]
    if ev.prerequisites != "not stated":
        detail.append(f"prerequisites: {ev.prerequisites}")
    if ev.age_limits != "not stated":
        detail.append(f"age: {ev.age_limits}")
    rows.append(f'<div style="font-size:12px;color:{INK};margin-top:4px">'
                + " · ".join(escape(d) for d in detail) + "</div>")
    for line in _stage_lines(ev):
        rows.append(f'<div style="font-size:12px;color:{MUT}">↳ {escape(line)}</div>')
    if ev.flags:
        rows.append('<div style="font-size:11px;margin-top:3px">'
                    + " ".join(f'<span style="border:1px solid {LINE};'
                               f'border-radius:8px;padding:1px 6px;color:{MUT}">'
                               f'{escape(f)}</span>' for f in dict.fromkeys(ev.flags))
                    + "</div>")
    rows.append(f'<div style="font-size:11px;color:{MUT};margin-top:3px">'
                f'CV line: “{escape(ev.cv_line)}” · confidence: {ev.confidence} · '
                f'source: {escape(ev.source)}</div>')
    return (f'<td style="padding:10px 14px;border-bottom:1px solid {LINE}">'
            + "".join(rows) + "</td>")


def _section(title: str, inner: str, note: str = "") -> str:
    if not inner:
        return ""
    n = (f'<div style="color:{MUT};font-size:12px;margin:2px 0 6px">'
         f'{escape(note)}</div>' if note else "")
    return (f'<tr><td style="padding:18px 14px 4px">'
            f'<div style="font:700 13px sans-serif;letter-spacing:.08em;'
            f'text-transform:uppercase;color:{BADGE}">{escape(title)}</div>{n}'
            f'</td></tr>{inner}')


def _cards(events: List[Event], leads: Optional[Dict[str, str]] = None) -> str:
    leads = leads or {}
    return "".join(f"<tr>{_card_html(e, leads.get(e.id, ''))}</tr>" for e in events)


# -------------------------------------------------------------------- build
def build(today: date, sunday: bool, *,
          act_now: List[Tuple[Event, Stage, str]],
          top: List[Event],
          worth_travel: List[Event],
          roundup: List[Event],
          low_conf: List[Event],
          diagnostics: dict,
          ask_reason_for: List[str]) -> Tuple[str, str, str]:
    """Returns (subject, html, plain_text)."""
    closing = {e.id for e in roundup + top + worth_travel
               if e.days_until_next_deadline is not None
               and e.days_until_next_deadline <= 7}
    closing |= {ev.id for ev, _, kind in act_now if kind in ("t7", "t2")}
    subject = subject_line(len(top), len(closing), sunday)

    act_leads, act_events, seen = {}, [], set()
    for ev, st, kind in act_now:
        label = {"opens": f"stage OPENS: {st.name}",
                 "t7": f"T-7 — {st.name} closes {st.closes}",
                 "t2": f"T-2 — {st.name} closes {st.closes}"}[kind]
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
            act_leads[e.id] = f"deadline in {e.days_until_next_deadline}d"
    top = [e for e in top if e.id not in seen]           # section 2 excludes section 1

    parts = []
    if ask_reason_for:
        parts.append(
            f'<tr><td style="padding:12px 14px;background:#fdf6ec;'
            f'border:1px solid {LINE}"><b>Quick question:</b> you marked '
            f'{escape(", ".join(ask_reason_for))} not-interested — reply with one '
            f'line why (too expensive / wrong field / too basic / too advanced / '
            f'bad timing / low prestige / dislike this format / dislike this '
            f'organiser) so the ranking learns the right lesson.</td></tr>')
    parts.append(_section("Act now — stages opening & deadlines ≤7 days",
                          _cards(act_events, act_leads)))
    parts.append(_section(f"Today's top {len(top)}", _cards(top)))
    parts.append(_section("Worth the travel",
                          _cards(worth_travel),
                          "funded or exceptionally prestigious — outside normal geography"))
    if sunday:
        inner = "".join(
            f'<tr><td style="padding:4px 14px;font-size:12px;'
            f'border-bottom:1px dotted {LINE}"><b>{e.id}</b> · '
            f'{"deadline " + str(e.next_deadline) if e.next_deadline else "no stated deadline"}'
            f' · <a href="{escape(e.url)}" style="color:{INK}">{escape(e.title)}</a>'
            f' — {escape(e.organiser)} · {escape(e.cost)} · '
            f'{e.format.replace("_", " ")}</td></tr>'
            for e in roundup)
        week = [e for e in roundup if e.days_until_next_deadline is not None
                and e.days_until_next_deadline <= 7]
        note = ("closing within 7 days: "
                + (", ".join(e.id for e in week) if week else "none")
                + f" — {len(roundup)} opportunities still open")
        parts.append(_section("Weekly roundup — everything still open", inner, note))
    parts.append(_section("Low confidence — snippet-only, verify before acting",
                          _cards(low_conf)))

    diag_lines = [
        f"sources queried: {diagnostics.get('sources_queried', 0)}",
        "sources errored: " + (", ".join(
            f"{k} ({v})" for k, v in diagnostics.get("sources_errored", {}).items())
            or "none"),
        f"Tavily credits used {diagnostics.get('tavily_used', 0)} / "
        f"remaining today {diagnostics.get('tavily_remaining', 0)}"
        + (" — cap exhausted, search sweep skipped"
           if diagnostics.get("tavily_exhausted") else ""),
        f"Gemini calls: {diagnostics.get('gemini_calls', 0)}"
        + (f" ({len(diagnostics.get('llm_dropped', []))} candidate pages dropped "
           f"at the LLM cap)" if diagnostics.get("llm_dropped") else ""),
        f"pages fetched: {diagnostics.get('fetched', 0)}, unchanged (304/hash): "
        f"{diagnostics.get('unchanged', 0)}",
        f"runtime: {diagnostics.get('runtime_s', 0)}s"
        + (" — HARD STOP at 20 min, partial results" if diagnostics.get("timed_out") else ""),
    ]
    if diagnostics.get("feedback_error"):
        diag_lines.append(f"IMAP feedback poll failed: {diagnostics['feedback_error']}")
    if diagnostics.get("gemini_errors"):
        diag_lines.append("Gemini errors: " + "; ".join(diagnostics["gemini_errors"][:3]))
    parts.append(_section("Diagnostics", "<tr><td style='padding:6px 14px;"
                          f"font-size:12px;color:{MUT}'>"
                          + "<br>".join(escape(x) for x in diag_lines) + "</td></tr>"))

    html = (f'<body style="margin:0;background:{BG};color:{INK};'
            f'font-family:Georgia,serif">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            f'<tr><td align="center"><table role="presentation" width="600" '
            f'style="max-width:600px;width:100%;background:#fff;'
            f'border:1px solid {LINE}" cellpadding="0" cellspacing="0">'
            f'<tr><td style="padding:16px 14px;border-bottom:2px solid {BADGE}">'
            f'<div style="font:700 18px Georgia,serif">Opportunity Scout</div>'
            f'<div style="color:{MUT};font-size:12px">{today.strftime("%A %d %B %Y")}'
            f'{" · Sunday weekly roundup" if sunday else ""}</div></td></tr>'
            + "".join(parts) +
            f'<tr><td style="padding:14px;color:{MUT};font-size:11px;'
            f'white-space:pre-line;border-top:1px solid {LINE}">'
            f'{escape(FOOTER_SYNTAX)}</td></tr>'
            f'</table></td></tr></table></body>')

    text = _plain_text(today, sunday, act_events, act_leads, top, worth_travel,
                       roundup, low_conf, diag_lines, ask_reason_for)
    return subject, html, text


def _plain_text(today, sunday, act_events, act_leads, top, worth_travel,
                roundup, low_conf, diag_lines, ask_reason_for) -> str:
    def line(e: Event, lead=""):
        dl = f"deadline {e.next_deadline}" if e.next_deadline else "deadline not stated"
        est = (" [estimates: " + "; ".join(
            f"{k}: {v}" for k, v in e.estimate_notes.items()) + "]"
            if e.estimate_notes else "")
        return (f"{e.id} {('[' + lead + '] ') if lead else ''}{e.title} — "
                f"{e.organiser} | {e.type} | {e.location} | {dl} | {e.cost} | "
                f"{e.url}{est}")

    out = [f"OPPORTUNITY SCOUT — {today}", ""]
    if ask_reason_for:
        out += [f"Why not-interested in {', '.join(ask_reason_for)}? One-line reply:"
                " too expensive / wrong field / too basic / too advanced / bad "
                "timing / low prestige / dislike this format / dislike this organiser", ""]
    if act_events:
        out += ["== ACT NOW =="] + [line(e, act_leads.get(e.id, "")) for e in act_events] + [""]
    if top:
        out += [f"== TODAY'S TOP {len(top)} =="] + [line(e) for e in top] + [""]
    if worth_travel:
        out += ["== WORTH THE TRAVEL =="] + [line(e) for e in worth_travel] + [""]
    if sunday:
        out += ["== WEEKLY ROUNDUP (everything still open) =="] + \
               [line(e) for e in roundup] + [""]
    if low_conf:
        out += ["== LOW CONFIDENCE (snippet-only) =="] + [line(e) for e in low_conf] + [""]
    out += ["== DIAGNOSTICS =="] + diag_lines + ["", FOOTER_SYNTAX]
    return "\n".join(out)


# --------------------------------------------------------------------- send
def send(subject: str, html: str, text: str, *, gmail_address: str,
         app_password: str, recipient: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = recipient
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(gmail_address, app_password)
        s.send_message(msg)
