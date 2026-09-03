#!/usr/bin/env python3
"""Opportunity Scout — daily orchestrator.

Pipeline: guard → load state/store → poll feedback → Tier 1 sources →
Tavily sweep (budgeted) → fetch LLM-candidate pages → batched Gemini →
classify/enrich → hard filters → dedup/resurface → score → render → send →
prune → persist. A failed run exits non-zero and sends nothing plausible;
a zero-event day still sends (sections 1, 4 on Sundays, and 6).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

import analysis
import email_render
import enrich as enrichment
import recurrence
import feedback
import filters
import score as scoring
from extract import GeminiBudget, llm_extract_batches
from models import Event
from sources.base import Fetcher, Geocoder
from sources.handlers import run_source
from store import State, Store

ROOT = Path(__file__).resolve().parent


class TavilyBudget:
    """Spends real money, so it refuses to spend any in --dry-run.

    A dry run does not save state, which means credits it burned would not be
    counted tomorrow — the counter would drift below the truth and the monthly
    cap would stop protecting the free tier. Cheaper and more honest to skip
    the paid calls entirely and say so in the diagnostics.
    """

    def __init__(self, state: State, today, cap: int, monthly_cap: int = 900,
                 enabled: bool = True):
        self.state, self.today = state, today
        self.cap, self.monthly_cap = cap, monthly_cap
        self.enabled = enabled

    def remaining(self) -> int:
        if not self.enabled:
            return 0
        return self.state.tavily_remaining(self.today, self.cap, self.monthly_cap)

    def take(self) -> bool:
        if self.remaining() <= 0:
            return False
        self.state.tavily_spend(1, self.today)
        return True


OUTBOX = ROOT / "data" / "outbox.json"


def _wait_until_send_time(cfg: dict) -> None:
    """Hold until the exact send time, then release.

    GitHub starts scheduled jobs late — observed 3 to 8 minutes on this repo,
    which is why a 06:30 cron produced 06:33 and 06:38 deliveries. So the cron
    fires early and this sleeps off the remainder. Only the leftover minutes
    are billed, and if GitHub was so late that the target has already passed,
    it sends immediately rather than waiting a whole day.
    """
    tz = ZoneInfo(cfg["settings"]["timezone"])
    hh, mm = str(cfg["settings"].get("send_at", "06:00")).split(":")
    now = datetime.now(tz)
    target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    wait = (target - now).total_seconds()
    cap = int(cfg["settings"].get("max_send_wait_min", 20)) * 60
    if wait <= 0:
        print(f"Target {target:%H:%M} already passed (now {now:%H:%M:%S}); "
              f"sending immediately.")
        return
    wait = min(wait, cap)
    print(f"Holding {int(wait)}s until {target:%H:%M} Europe/Lisbon.")
    time.sleep(wait)


def deliver(cfg: dict) -> int:
    """Send the digest --prepare parked earlier this morning.

    Deliberately loud on failure: an empty or stale outbox means the prepare
    job broke, and a silent success would hide that behind a missing email.
    """
    gmail = os.environ.get("GMAIL_ADDRESS", "")
    app_pw = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not OUTBOX.exists():
        print("No digest was prepared this morning — the 06:10 job must have "
              "failed. Nothing to send.", file=sys.stderr)
        return 1
    payload = json.loads(OUTBOX.read_text(encoding="utf-8"))
    today = datetime.now(ZoneInfo(cfg["settings"]["timezone"])).date()
    if payload.get("date") != today.isoformat():
        print(f"Outbox holds a digest from {payload.get('date')}, not today "
              f"({today}). Refusing to send stale mail.", file=sys.stderr)
        return 1
    if os.environ.get("GITHUB_EVENT_NAME", "") == "schedule":
        _wait_until_send_time(cfg)      # manual runs send straight away
    email_render.send(payload["subject"], payload["html"], payload["text"],
                      gmail_address=gmail, app_password=app_pw,
                      recipient=payload["recipient"])
    OUTBOX.unlink()
    print(f"Delivered '{payload['subject']}' to {payload['recipient']}.")
    return 0


def load_cfg():
    fcfg = yaml.safe_load((ROOT / "config" / "filters.yaml").read_text(encoding="utf-8"))
    scfg = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"))
    return fcfg, scfg["sources"]


def guard(cfg: dict, args) -> bool:
    """Two crons fire year-round to survive DST; only the one landing inside
    the local target-hour window runs. Manual workflow_dispatch, local runs
    and --dry-run bypass the guard."""
    if args.dry_run or os.environ.get("GITHUB_EVENT_NAME", "") != "schedule":
        return True
    tz = ZoneInfo(cfg["settings"]["timezone"])
    return datetime.now(tz).hour == int(cfg["settings"]["target_hour"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Opportunity Scout")
    ap.add_argument("--dry-run", action="store_true", help="print, don't send; no state writes")
    ap.add_argument("--limit", type=int, default=None, help="cap on Today's Top section")
    ap.add_argument("--source", default=None, help="run only this source name")
    ap.add_argument("--backfill", action="store_true",
                    help="first run: raise per-source caps to sweep the backlog")
    ap.add_argument("--prepare", action="store_true",
                    help="do the work and park the finished digest in data/outbox.json")
    ap.add_argument("--deliver", action="store_true",
                    help="send whatever --prepare parked, then clear the outbox")
    args = ap.parse_args(argv)

    cfg, sources = load_cfg()
    filters.prime(cfg)

    if args.deliver:
        return deliver(cfg)
    if not guard(cfg, args):
        print("Outside the 06:00–07:00 Europe/Lisbon window for this cron — exiting.")
        return 0

    tz = ZoneInfo(cfg["settings"]["timezone"])
    now = datetime.now(tz)
    today = now.date()
    sunday = today.weekday() == 6
    start = time.monotonic()
    deadline = start + cfg["settings"]["max_runtime_min"] * 60
    if args.backfill:
        cfg["settings"]["max_detail_pages_per_source"] *= \
            cfg["settings"]["backfill_detail_multiplier"]

    state = State(ROOT / "data" / "state.json")
    store = Store(ROOT / "data" / "seen.jsonl", ROOT / "data" / "events.db", state)
    diag = {"sources_queried": 0, "sources_errored": {}, "rejected": {}}

    gmail = os.environ.get("GMAIL_ADDRESS", "")
    app_pw = os.environ.get("GMAIL_APP_PASSWORD", "")
    recipient = os.environ.get("RECIPIENT_EMAIL", "mabcec2019@gmail.com")

    # 1. feedback first, so today's digest reflects it
    if not args.dry_run and gmail and app_pw:
        feedback.poll_inbox(store, state, cfg, today,
                            gmail_address=gmail, app_password=app_pw,
                            diagnostics=diag)

    # 2. gather
    fetcher = Fetcher(state, gmail, deadline)
    geocoder = Geocoder(fetcher, state)

    # 1b. Apply today's filters to yesterday's stored events. This runs AFTER
    # the geocoder exists: without one, every stored event whose town was
    # originally resolved by Nominatim fails as "location unverifiable" and is
    # deleted — a silent, permanent data loss that looked like a clean-up.
    store.reset_derived_flags()

    def _still_valid(ev):
        if ev.confidence == "low":
            return filters.relevance_filter(ev, cfg, today)
        return filters.apply_all(ev, cfg, geocoder, today)

    released = store.release_eligible(today)
    if released:
        diag["released"] = [f"{e.id} {e.title[:40]}" for e in released]

    evicted = store.revalidate(_still_valid, today)
    if evicted:
        diag["evicted"] = evicted
        print(f"Retired {len(evicted)} stored events that no longer pass filters")
    tavily_budget = TavilyBudget(state, today,
                                 cfg["settings"]["tavily_daily_cap"],
                                 cfg["settings"]["tavily_monthly_cap"],
                                 enabled=not args.dry_run)
    if args.dry_run:
        diag["tavily_note"] = "skipped in --dry-run (paid calls, unsaved counter)"
    raw_events: list[Event] = []
    llm_candidates: list[dict] = []
    for src in sorted(sources, key=lambda s: s.get("tier", 1)):
        if args.source and src["name"] != args.source:
            continue
        if not src.get("enabled", True):
            continue
        if fetcher.out_of_time():
            diag["timed_out"] = True
            break
        if src.get("tier") == 2 and \
                tavily_budget.remaining() <= 0:
            diag["tavily_exhausted"] = True
            continue
        diag["sources_queried"] += 1
        try:
            res = run_source(src, fetcher, cfg,
                             os.environ.get("TAVILY_API_KEY", ""), tavily_budget)
            raw_events += res.events
            llm_candidates += res.llm_candidates
            if not res.ok or res.error:
                diag["sources_errored"][src["name"]] = res.error or "failed"
        except Exception as e:                     # noqa: BLE001 — one dead source never kills a run
            diag["sources_errored"][src["name"]] = f"{type(e).__name__}: {e}"[:150]

    # 3. fetch full pages for Tavily hits that allowed it, then batched LLM
    for c in llm_candidates:
        if c.pop("needs_fetch", False) and not fetcher.out_of_time():
            status, body, changed = fetcher.get(c["url"])
            if body:
                from bs4 import BeautifulSoup
                c["text"] = BeautifulSoup(body, "html.parser").get_text(" ", strip=True)
                c["http_status"] = status
    gemini = GeminiBudget(cfg["settings"]["gemini_run_cap"])
    llm_events, dropped = llm_extract_batches(
        llm_candidates, os.environ.get("GEMINI_API_KEY", ""),
        cfg["settings"]["gemini_model"], gemini,
        cfg["settings"]["gemini_batch_size"],
        cfg["settings"]["gemini_seconds_between_calls"], diag)
    raw_events += llm_events
    if dropped:
        diag["llm_dropped"] = [c["url"] for c in dropped]

    # 4. enrich → filter → store → score
    low_conf: list[Event] = []
    new_events: list[Event] = []

    def process(events, low_conf=low_conf, new_events=new_events):
        """Classify, filter and store a batch. Used by the main pass and again
        by the quiet-week fallback, so both get identical treatment."""
        for ev in events:
            try:
                filters.classify(ev, cfg)
                scoring.enrich_estimates(ev, cfg)
                if ev.confidence == "low":
                    # Snippets skip geography/calendar (no facts to test) but
                    # must still clear relevance, or the digest fills with
                    # reminiscing about events that already happened.
                    ok, reason = filters.relevance_filter(ev, cfg, today)
                    if not ok:
                        key = reason.split(":")[0]
                        diag["rejected"][key] = diag["rejected"].get(key, 0) + 1
                        continue
                    status, canonical = store.upsert(ev, today)
                    if status == "new":
                        low_conf.append(canonical)
                    continue
                ok, reason = filters.apply_all(ev, cfg, geocoder, today)
                if not ok:
                    key = reason.split(":")[0]
                    diag["rejected"][key] = diag["rejected"].get(key, 0) + 1
                    # "Too early" is a date, not a no. Park it and release it
                    # when the date arrives, rather than discarding a target.
                    if key == "eligibility" and ev.eligible_from:
                        store.defer(ev, today)
                    continue
                status, canonical = store.upsert(ev, today)
                if status == "new" or status.startswith("resurfaced"):
                    if status.startswith("resurfaced"):
                        canonical.flags.append(status.replace("resurfaced:", "changed: "))
                    new_events.append(canonical)
            except Exception as e:                 # noqa: BLE001
                diag["rejected"]["error"] = diag["rejected"].get("error", 0) + 1
                diag.setdefault("event_errors", []).append(str(e)[:120])

    process(raw_events)

    # 4a. Quiet week? Spend leftover search budget rather than sending a thin
    # digest. The broader queries live in sources.yaml and are deliberately
    # vaguer than the daily sweep — they trade precision for coverage, which is
    # the right trade only when the precise ones came back empty.
    threshold = int(cfg["settings"].get("thin_digest_threshold", 6))
    qualifying = [e for e in store.open.values() if e.confidence != "low"]
    if (len(qualifying) < threshold and not fetcher.out_of_time()
            and tavily_budget.remaining() > 0):
        fallback = [s for s in sources
                    if s.get("type") == "tavily" and s.get("fallback")]
        extra_raw, extra_llm = [], []
        for src in fallback:
            if fetcher.out_of_time() or tavily_budget.remaining() <= 0:
                break
            try:
                res = run_source(src, fetcher, cfg,
                                 os.environ.get("TAVILY_API_KEY", ""), tavily_budget)
                extra_raw += res.events
                extra_llm += res.llm_candidates
            except Exception as e:                 # noqa: BLE001
                diag["sources_errored"][src["name"]] = f"{type(e).__name__}: {e}"[:150]
        for c in extra_llm:
            if c.pop("needs_fetch", False) and not fetcher.out_of_time():
                status, body, _ = fetcher.get(c["url"])
                if body:
                    from bs4 import BeautifulSoup
                    c["text"] = BeautifulSoup(body, "html.parser").get_text(" ", strip=True)
                    c["http_status"] = status
        more, _dropped = llm_extract_batches(
            extra_llm, os.environ.get("GEMINI_API_KEY", ""),
            diag.get("gemini_model_used") or cfg["settings"]["gemini_model"],
            gemini, cfg["settings"]["gemini_batch_size"],
            cfg["settings"]["gemini_seconds_between_calls"], diag)
        before = len(qualifying)
        process(extra_raw + more)
        after = len([e for e in store.open.values() if e.confidence != "low"])
        diag["fallback"] = (f"only {before} events cleared the filters (under "
                            f"{threshold}), so a broader sweep ran and added "
                            f"{after - before}")

    downweights = state.data["downweights"]
    for ev in list(store.open.values()):
        scoring.score(ev, cfg, today, downweights)

    # 4b. Second opinion. Rank once to find the events worth spending a search
    # on, fill their gaps from independent sources, then re-rank — a prestige
    # score corrected from 2 to 4 has to be able to move an event up the list.
    prelim = sorted((e for e in store.open.values()),
                    key=lambda e: -e.score)[:cfg["settings"]["top_section_cap"] * 2]
    enrichment.enrich(prelim, cfg,
                      os.environ.get("TAVILY_API_KEY", ""),
                      os.environ.get("GEMINI_API_KEY", ""),
                      diag.get("gemini_model_used") or cfg["settings"]["gemini_model"],
                      tavily_budget, gemini, diag,
                      out_of_time=fetcher.out_of_time)
    twins = enrichment.mark_domestic_twins(list(store.open.values()), cfg)
    not_yet = store.upcoming_eligibility(
        today, int(cfg["settings"].get("eligibility_horizon_days", 240)))
    # Seasonal memory: note this year's dates, then look for families that
    # opened around now in a previous year and have not reappeared yet.
    recurrence.record(list(store.open.values()), state.data, today)
    open_keys = {enrichment.family_key(e) for e in store.open.values()}
    diag["expected_soon"] = recurrence.as_prompts(state.data, today, open_keys)
    if twins:
        diag["domestic_twins"] = twins
    for ev in list(store.open.values()):
        scoring.score(ev, cfg, today, downweights)
    # Score first, then soonest-first inside a tie — otherwise equally-scored
    # events come out in arbitrary order and the list looks shuffled.
    far = date(2100, 1, 1)
    new_events = sorted({e.id: e for e in new_events}.values(),
                        key=lambda e: (-e.score, e.start_date or far,
                                       e.next_deadline or far))

    # 5. sections
    act_now = store.stage_alerts(today)
    top_cap = args.limit or cfg["settings"]["top_section_cap"]
    low_cap = cfg["settings"]["low_confidence_cap"]
    low_conf = sorted(low_conf, key=lambda e: -e.score)[:low_cap]
    new_ids = {e.id for e in new_events}
    for e in store.open.values():
        if e.id in new_ids:
            e.flags.append("new today")

    # The shortlist is the best N open right now, not merely what turned up in
    # the last 24h — on a quiet day "3 new" is not the same as "3 worth doing".
    far = date(2100, 1, 1)
    ranked = sorted((e for e in store.open.values() if e.confidence != "low"),
                    key=lambda e: (-e.score, e.start_date or far,
                                   e.next_deadline or far))
    alerted = {ev.id for ev, _, _ in act_now}
    pinned = [e for e in ranked if e.pinned]
    pinned_ids = {e.id for e in pinned}
    worth = [e for e in ranked if "worth the travel" in e.flags
             and e.id not in alerted and e.id not in pinned_ids]
    top = [e for e in ranked if e not in worth and e.id not in alerted
           and e.id not in pinned_ids][:top_cap]

    # Close the loop on things you said you liked, once each, as the deadline
    # arrives or just after it.
    ask_applied = []
    for e in store.open.values():
        near = e.days_until_next_deadline
        if (e.applied is None and not e.asked_applied
                and ("you marked this interesting" in e.flags or e.pinned)
                and near is not None and near <= 1):
            e.asked_applied = True
            ask_applied.append(e)

    if sunday:
        # Sunday is a complete inventory: every single thing still open that you
        # have not rejected, including the ones shown as cards above and the
        # unverified snippets. Nothing is filtered out of this list.
        roundup = sorted(store.open.values(),
                         key=lambda e: (e.next_deadline or far,
                                        -e.score))
    else:
        # Weekdays: everything open that did not make a full card, so a good
        # opportunity is never invisible just because it ranked eleventh.
        shown = {e.id for e in top + worth + low_conf + pinned} | alerted
        roundup = [e for e in ranked if e.id not in shown]
    diag.update(open_total=len(store.open),
                dismissed_total=len(store.dismissed),
                deferred_total=len(store.deferred),
                evicted_count=len(diag.get("evicted", [])),
                tavily_used=state.data["tavily"]["used"],
                tavily_remaining=tavily_budget.remaining(),
                tavily_month=state.data["tavily"].get("month_used", 0),
                gemini_calls=gemini.used, fetched=fetcher.fetched,
                unchanged=fetcher.unchanged,
                runtime_s=int(time.monotonic() - start))
    if time.monotonic() > deadline:
        diag["timed_out"] = True

    subject, html, text = email_render.build(
        today, sunday, act_now=act_now, top=top, worth_travel=worth,
        roundup=roundup, low_conf=low_conf, pinned=pinned, not_yet=not_yet,
        ask_applied=ask_applied, expected_soon=diag["expected_soon"],
        diagnostics=diag, ask_reason_for=list(state.data["ask_reason_for"]),
        reply_to=gmail or recipient)

    # 6. send / print — a missing email must always mean the job broke
    if args.dry_run:
        print(text)
        print(f"\n[dry-run] subject: {subject}")
        print(f"[dry-run] html: {len(html)} bytes; no email sent, no state written")
        store.close()
        return 0
    if args.prepare:
        # Park it. The 06:30 cron picks it up in a ~40-second job, which costs
        # far less than holding this one open for twenty idle minutes.
        OUTBOX.parent.mkdir(parents=True, exist_ok=True)
        OUTBOX.write_text(json.dumps(
            {"prepared_at": datetime.now(tz).isoformat(), "date": today.isoformat(),
             "subject": subject, "html": html, "text": text,
             "recipient": recipient}, ensure_ascii=False), encoding="utf-8")
        print(f"Prepared '{subject}' -> {OUTBOX} (delivery job will send it)")
    else:
        email_render.send(subject, html, text, gmail_address=gmail,
                          app_password=app_pw, recipient=recipient)

    # 7. persist — seen.jsonl is the durable truth, committed by the workflow
    store.prune(today)
    store.save()
    state.save()
    store.close()
    print(f"{'Queued' if args.prepare else 'Sent'} '{subject}' for {recipient}; "
          f"{len(store.open)} open, {len(store.dismissed)} dismissed on record.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
