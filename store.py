"""Durable state.

data/seen.jsonl  — source of truth, committed to the repo every run.
                   Full records for every open event; hash lines (with the
                   minimal keys needed for fuzzy matching and pruning) for
                   dismissed events.
data/events.db   — ephemeral SQLite working copy, rebuilt from seen.jsonl at
                   the start of every run. Deleting it must never lose data.
data/state.json  — counters and caches: next event id, Tavily daily counter,
                   ETag/Last-Modified cache, geocode cache, feedback weights.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import date, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from models import STICKY_FLAGS, Event, Stage

FUZZY_THRESHOLD = 0.90
FUZZY_WINDOW_DAYS = 90
PRUNE_AFTER_DAYS = 60


def normalise(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def event_hash(title: str, organiser: str, start_date: Optional[date]) -> str:
    key = f"{normalise(title)}|{normalise(organiser)}|{start_date.isoformat() if start_date else ''}"
    return hashlib.sha256(key.encode()).hexdigest()


def default_state() -> dict:
    return {
        "next_event_id": 1,
        "tavily": {"date": "", "used": 0, "month": "", "month_used": 0},
        "etag_cache": {},        # url -> {etag, last_modified, body_sha256}
        "geocode_cache": {},     # normalised place -> {municipality, country}
        "downweights": {},       # "type:hackathon" / "organiser:x" / "field:y" -> count
        "feedback_reasons": [],  # [{event_id, reason, date}]
        "ask_reason_for": [],
        "last_imap_uid": 0,
        "last_imap_uid_folder": 0,
        "outcomes": [],
        "recurrence": {},    # event ids to prompt a reason for, top of next digest
    }


class State:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = default_state()
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text()))
            except Exception:
                pass  # corrupt state must not kill the run; start fresh

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1, sort_keys=True))

    # Tavily daily budget -------------------------------------------------
    def _tavily_roll(self, today: date) -> dict:
        """Roll the day and month windows. Called by both remaining() and
        spend(), so a spend can never land in a stale window and vanish."""
        t = self.data["tavily"]
        if t.get("date") != today.isoformat():
            t["date"], t["used"] = today.isoformat(), 0
        month = today.strftime("%Y-%m")
        if t.get("month") != month:
            t["month"], t["month_used"] = month, 0
        return t

    def tavily_remaining(self, today: date, cap: int,
                         monthly_cap: int = 900) -> int:
        """Two ceilings. The daily one leaves room for manual re-runs; the
        monthly one is what actually protects the free tier (1,000/month)."""
        t = self._tavily_roll(today)
        return max(0, min(cap - t["used"], monthly_cap - t["month_used"]))

    def tavily_spend(self, n: int = 1, today: Optional[date] = None) -> None:
        t = self._tavily_roll(today or date.today())
        t["used"] += n
        t["month_used"] = t.get("month_used", 0) + n


class Store:
    def __init__(self, seen_path: Path, db_path: Path, state: State):
        self.seen_path = Path(seen_path)
        self.state = state
        self.open: Dict[str, Event] = {}
        self.dismissed: Dict[str, dict] = {}
        self.deferred: Dict[str, Event] = {}   # too early now, dated for later
        self._load()
        # Ephemeral SQLite rebuilt from seen.jsonl — losing it loses nothing.
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        if db_path.exists():
            db_path.unlink()
        self.db = sqlite3.connect(db_path)
        self.db.execute(
            "CREATE TABLE events (hash TEXT PRIMARY KEY, title_norm TEXT,"
            " organiser_norm TEXT, first_seen TEXT, dismissed INTEGER, json TEXT)"
        )
        for h, ev in self.open.items():
            self._db_put(h, normalise(ev.title), normalise(ev.organiser),
                         ev.first_seen_date, 0, ev.model_dump_json())
        for h, rec in self.dismissed.items():
            self._db_put(h, rec.get("title_norm", ""), rec.get("organiser_norm", ""),
                         rec.get("first_seen"), 1, json.dumps(rec))
        self.db.commit()

    def _db_put(self, h, tn, on, fs, dis, js):
        fs = fs.isoformat() if isinstance(fs, date) else (fs or "")
        self.db.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?)",
                        (h, tn, on, fs, dis, js))

    def _load(self) -> None:
        if not self.seen_path.exists():
            return
        for line in self.seen_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("dismissed"):
                self.dismissed[rec["hash"]] = rec
            elif rec.get("deferred"):
                ev = Event.model_validate(rec["event"])
                self.deferred[ev.dedup_hash] = ev
            else:
                ev = Event.model_validate(rec)
                self.open[ev.dedup_hash] = ev

    # ------------------------------------------------------------------ ids
    def _assign_id(self, ev: Event) -> None:
        n = self.state.data["next_event_id"]
        ev.id = f"E-{n:04d}"
        self.state.data["next_event_id"] = n + 1

    # ---------------------------------------------------------------- dedup
    def _fuzzy_hit(self, title: str, today: date) -> Optional[str]:
        tn = normalise(title)
        floor = (today - timedelta(days=FUZZY_WINDOW_DAYS)).isoformat()
        for h, existing, fs in self.db.execute(
                "SELECT hash, title_norm, first_seen FROM events WHERE first_seen >= ?",
                (floor,)):
            if existing and SequenceMatcher(None, tn, existing).ratio() >= FUZZY_THRESHOLD:
                return h
        return None

    @staticmethod
    def _material_change(old: Event, new: Event) -> List[str]:
        changed = []
        if new.start_date and new.start_date != old.start_date:
            changed.append("start date")
        if new.end_date and new.end_date != old.end_date:
            changed.append("end date")
        if new.next_deadline and new.next_deadline != old.next_deadline:
            changed.append("deadline")
        if new.cost != "not stated" and new.cost != old.cost:
            changed.append("cost")
        return changed

    def upsert(self, ev: Event, today: date) -> Tuple[str, Event]:
        """Returns (status, canonical_event).
        status: new | duplicate | resurfaced:<reasons> | suppressed
        User dismissal (not-interested / cant-attend) is permanent for that
        instance and is NOT overridden by stage resurfacing.
        """
        h = ev.dedup_hash = event_hash(ev.title, ev.organiser, ev.start_date)
        hit = h if (h in self.open or h in self.dismissed) else self._fuzzy_hit(ev.title, today)
        if hit and hit in self.dismissed:
            return "suppressed", ev
        if hit and hit in self.open:
            old = self.open[hit]
            changes = self._material_change(old, ev)
            # merge fresh facts onto the stored record, keep id / first_seen / alerts
            merged = old.model_copy(update={
                k: v for k, v in ev.model_dump().items()
                if k not in ("id", "first_seen_date", "dedup_hash", "stages", "flags",
                             "score", "estimate_notes")
                and v not in (None, "", [], "not stated", "none stated")
            })
            merged.stages = self._merge_stages(old.stages, ev.stages)
            merged.last_seen_date = today
            self.open[hit] = merged
            if changes:
                return "resurfaced:" + ", ".join(changes), merged
            return "duplicate", merged
        # brand new
        self._assign_id(ev)
        ev.first_seen_date = ev.last_seen_date = today
        self.open[h] = ev
        self._db_put(h, normalise(ev.title), normalise(ev.organiser), today, 0,
                     ev.model_dump_json())
        return "new", ev

    @staticmethod
    def _merge_stages(old: List[Stage], new: List[Stage]) -> List[Stage]:
        by_name = {s.name: s for s in old}
        for s in new:
            if s.name in by_name:
                kept = by_name[s.name]
                kept.opens = s.opens or kept.opens
                kept.closes = s.closes or kept.closes
                kept.is_advantageous = kept.is_advantageous or s.is_advantageous
            else:
                by_name[s.name] = s
        return list(by_name.values())

    # -------------------------------------------------------- stage alerts
    def stage_alerts(self, today: date) -> List[Tuple[Event, Stage, str]]:
        """Alerts fired once each: when a stage opens, at T-7, at T-2.
        This is the resurfacing override: a seen event MUST come back when a
        stage opens or enters its T-7 window."""
        out = []
        for ev in self.open.values():
            for st in ev.stages:
                if st.opens and st.opens <= today and "opens" not in st.alerts_sent \
                        and (not st.closes or st.closes >= today):
                    st.alerts_sent.append("opens")
                    out.append((ev, st, "opens"))
                if st.closes:
                    d = (st.closes - today).days
                    if 0 <= d <= 7 and "t7" not in st.alerts_sent:
                        st.alerts_sent.append("t7")
                        out.append((ev, st, "t7"))
                    if 0 <= d <= 2 and "t2" not in st.alerts_sent:
                        st.alerts_sent.append("t2")
                        out.append((ev, st, "t2"))
        return out

    # ------------------------------------------------------------ feedback
    def find_by_id(self, event_id: str) -> Optional[Event]:
        eid = event_id.upper()
        for ev in self.open.values():
            if ev.id == eid:
                return ev
        return None

    def dismiss(self, ev: Event, kind: str, today: date) -> None:
        """kind: not_interested (permanent + downweight) | cant_attend (instance only)."""
        h = ev.dedup_hash
        self.open.pop(h, None)
        self.dismissed[h] = {
            "hash": h, "dismissed": kind, "id": ev.id,
            "title_norm": normalise(ev.title), "organiser_norm": normalise(ev.organiser),
            "first_seen": (ev.first_seen_date or today).isoformat(),
            "deadline": ev.next_deadline.isoformat() if ev.next_deadline else None,
        }
        self._db_put(h, normalise(ev.title), normalise(ev.organiser),
                     ev.first_seen_date, 1, json.dumps(self.dismissed[h]))

    # --------------------------------------------------------------- prune
    # --------------------------------------------------- not yet, but dated
    def defer(self, ev: Event, today: date) -> str:
        """Park something you are simply too early for.

        Rejecting CERN outright throws away the fact that it is a real target
        for 2029. Deferred events are kept whole, out of the digest, and
        released automatically once the date arrives.
        """
        if not ev.dedup_hash:
            ev.dedup_hash = event_hash(ev.title, ev.organiser, ev.start_date)
        if ev.dedup_hash in self.dismissed:
            return "suppressed"              # you said no; that still wins
        existing = self.deferred.get(ev.dedup_hash)
        if existing:
            existing.last_seen_date = today
            if ev.eligible_from:
                existing.eligible_from = ev.eligible_from
            return "deferred"
        if not ev.id:
            self._assign_id(ev)
        ev.last_seen_date = today
        self.deferred[ev.dedup_hash] = ev
        return "deferred"

    def release_eligible(self, today: date) -> List[Event]:
        """Move anything whose date has arrived back into the digest."""
        out = []
        for h, ev in list(self.deferred.items()):
            if ev.eligible_from and ev.eligible_from <= today:
                del self.deferred[h]
                ev.flags.append("you are now eligible for this")
                self.open[h] = ev
                out.append(ev)
        return out

    def upcoming_eligibility(self, today: date, within_days: int = 240) -> List[Event]:
        """Deferred events worth mentioning because the date is approaching."""
        soon = [e for e in self.deferred.values()
                if e.eligible_from
                and 0 <= (e.eligible_from - today).days <= within_days]
        return sorted(soon, key=lambda e: e.eligible_from)

    def reset_derived_flags(self) -> None:
        """Drop everything the pipeline will re-derive this run."""
        for ev in list(self.open.values()) + list(self.deferred.values()):
            ev.flags = [f for f in ev.flags if f in STICKY_FLAGS]

    def revalidate(self, predicate, today: date) -> List[str]:
        """Re-test every stored open event against the current filters.

        Without this, tightening a rule only ever affects events that have not
        been seen yet: anything already in seen.jsonl keeps surfacing in the
        roundup forever. The pub crawls and psychic readings that survived the
        first run were exactly this. Evicted events are recorded as dismissed
        so they cannot be re-ingested tomorrow.
        """
        evicted = []
        for eid, ev in list(self.open.items()):
            ok, reason = predicate(ev)
            if ok:
                continue
            # Report by ID and title; the dict key is a content hash and means
            # nothing to a human reading the diagnostics section.
            evicted.append(f"{ev.id} {ev.title[:44]} — {reason}")
            self.dismiss(ev, "filtered_out", today)
        return evicted

    def prune(self, today: date) -> int:
        cutoff = today - timedelta(days=PRUNE_AFTER_DAYS)
        n = 0
        for h, ev in list(self.open.items()):
            dl = ev.next_deadline or ev.end_date
            if dl and dl < cutoff:
                del self.open[h]
                n += 1
        for h, rec in list(self.dismissed.items()):
            dl = rec.get("deadline")
            if dl and date.fromisoformat(dl) < cutoff:
                del self.dismissed[h]
                n += 1
        return n

    # ---------------------------------------------------------------- save
    def save(self) -> None:
        self.seen_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [e.model_dump_json(exclude_none=True)
                 for e in sorted(self.open.values(), key=lambda e: e.id)]
        lines += [json.dumps({"deferred": True,
                              "event": json.loads(e.model_dump_json(exclude_none=True))})
                  for e in sorted(self.deferred.values(), key=lambda e: e.id)]
        lines += [json.dumps(r) for r in self.dismissed.values()]
        self.seen_path.write_text("\n".join(lines) + ("\n" if lines else ""))
