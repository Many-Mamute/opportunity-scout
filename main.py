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
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

import email_render
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
    def __init__(self, state: State, today, cap: int):
        self.state, self.today, self.cap = state, today, cap

    def take(self) -> bool:
        if self.state.tavily_remaining(self.today, self.cap) <= 0:
            return False
        self.state.tavily_spend(1)
        return True


def load_cfg():
    fcfg = yaml.safe_load((ROOT / "config" / "filters.yaml").read_text())
    scfg = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text())
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
    args = ap.parse_args(argv)

    cfg, sources = load_cfg()
    filters.prime(cfg)
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
    tavily_budget = TavilyBudget(state, today,
                                 cfg["settings"]["tavily_daily_cap"])
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
                state.tavily_remaining(today, tavily_budget.cap) <= 0:
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
    for ev in raw_events:
        try:
            filters.classify(ev, cfg)
            scoring.enrich_estimates(ev, cfg)
            if ev.confidence == "low":
                status, canonical = store.upsert(ev, today)
                if status == "new":
                    low_conf.append(canonical)
                continue
            ok, reason = filters.apply_all(ev, cfg, geocoder)
            if not ok:
                key = reason.split(":")[0]
                diag["rejected"][key] = diag["rejected"].get(key, 0) + 1
                continue
            status, canonical = store.upsert(ev, today)
            if status == "new" or status.startswith("resurfaced"):
                if status.startswith("resurfaced"):
                    canonical.flags.append(status.replace("resurfaced:", "changed: "))
                new_events.append(canonical)
        except Exception as e:                     # noqa: BLE001
            diag["rejected"]["error"] = diag["rejected"].get("error", 0) + 1
            diag.setdefault("event_errors", []).append(str(e)[:120])

    downweights = state.data["downweights"]
    for ev in list(store.open.values()):
        scoring.score(ev, cfg, today, downweights)
    new_events = sorted({e.id: e for e in new_events}.values(),
                        key=lambda e: e.score, reverse=True)

    # 5. sections
    act_now = store.stage_alerts(today)
    cap = args.limit or cfg["settings"]["top_section_cap"]
    worth = [e for e in new_events if "worth the travel" in e.flags]
    top = [e for e in new_events if e not in worth][:cap]
    roundup = sorted((e for e in store.open.values() if e.confidence != "low"),
                     key=lambda e: (e.next_deadline is None, e.next_deadline or today))
    diag.update(tavily_used=state.data["tavily"]["used"],
                tavily_remaining=state.tavily_remaining(today, tavily_budget.cap),
                gemini_calls=gemini.used, fetched=fetcher.fetched,
                unchanged=fetcher.unchanged,
                runtime_s=int(time.monotonic() - start))
    if time.monotonic() > deadline:
        diag["timed_out"] = True

    subject, html, text = email_render.build(
        today, sunday, act_now=act_now, top=top, worth_travel=worth,
        roundup=roundup if sunday else [], low_conf=low_conf,
        diagnostics=diag, ask_reason_for=list(state.data["ask_reason_for"]))

    # 6. send / print — a missing email must always mean the job broke
    if args.dry_run:
        print(text)
        print(f"\n[dry-run] subject: {subject}")
        print(f"[dry-run] html: {len(html)} bytes; no email sent, no state written")
        return 0
    email_render.send(subject, html, text, gmail_address=gmail,
                      app_password=app_pw, recipient=recipient)

    # 7. persist — seen.jsonl is the durable truth, committed by the workflow
    store.prune(today)
    store.save()
    state.save()
    print(f"Sent '{subject}' to {recipient}; "
          f"{len(store.open)} open, {len(store.dismissed)} dismissed on record.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
