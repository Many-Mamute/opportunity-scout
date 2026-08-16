"""Polite HTTP layer shared by every source.

- descriptive User-Agent with contact email
- robots.txt honoured (cached per domain per run)
- max 1 request/second per domain
- exponential backoff on 429/5xx (honours Retry-After)
- HTTP conditional requests: If-None-Match / If-Modified-Since from the
  persisted cache; where headers are absent, the body hash is compared, so an
  unchanged source is skipped either way.
"""
from __future__ import annotations

import hashlib
import time
import urllib.robotparser
from typing import Optional, Tuple
from urllib.parse import urlparse

import requests

from store import State, normalise


class Fetcher:
    def __init__(self, state: State, contact_email: str, deadline: float):
        self.state = state
        self.deadline = deadline                  # time.monotonic() hard stop
        self.session = requests.Session()
        self.session.headers.update({
            # Honest, identifying UA with a contact address, per the politeness
            # rules. The Accept headers are not disguise — plenty of CDNs 403 a
            # request that sends none at all, which is what noticias.up.pt did.
            "User-Agent": ("OpportunityScout/1.0 (personal daily opportunity "
                           f"aggregator; contact: {contact_email or 'not-configured'})"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
        })
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._last_hit: dict[str, float] = {}
        self.fetched = 0
        self.unchanged = 0

    def out_of_time(self) -> bool:
        return time.monotonic() > self.deadline

    # ------------------------------------------------------------- politeness
    def _robots_ok(self, url: str) -> bool:
        host = urlparse(url).netloc
        if host not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.session.get(f"https://{host}/robots.txt", timeout=8)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except requests.RequestException:
                rp = None                          # unreachable robots ≠ ban
            self._robots[host] = rp
        rp = self._robots[host]
        return True if rp is None else rp.can_fetch(
            self.session.headers["User-Agent"], url)

    def _throttle(self, url: str) -> None:
        host = urlparse(url).netloc
        wait = 1.0 - (time.monotonic() - self._last_hit.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    # ------------------------------------------------------------------- get
    def get(self, url: str, timeout: int = 20
            ) -> Tuple[Optional[int], Optional[str], bool]:
        """Returns (status, text, changed). text is None when robots-blocked,
        errored, or unchanged since last run (changed=False)."""
        if self.out_of_time():
            return None, None, False
        if not self._robots_ok(url):
            return 999, None, False               # sentinel: robots-blocked
        cache = self.state.data["etag_cache"].get(url, {})
        headers = {}
        if cache.get("etag"):
            headers["If-None-Match"] = cache["etag"]
        if cache.get("last_modified"):
            headers["If-Modified-Since"] = cache["last_modified"]
        for attempt in range(3):
            self._throttle(url)
            try:
                r = self.session.get(url, headers=headers, timeout=timeout)
            except requests.RequestException:
                if attempt == 2:
                    return None, None, False
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 304:
                self.unchanged += 1
                return 304, None, False
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == 2:
                    return r.status_code, None, False
                retry = r.headers.get("Retry-After")
                time.sleep(min(30, float(retry)) if retry and retry.isdigit()
                           else 2 ** (attempt + 1))
                continue
            if r.status_code >= 400:
                return r.status_code, None, False
            body = r.text
            digest = hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()
            entry = {"etag": r.headers.get("ETag"),
                     "last_modified": r.headers.get("Last-Modified"),
                     "body_sha256": digest}
            unchanged = cache.get("body_sha256") == digest
            self.state.data["etag_cache"][url] = entry
            if unchanged:
                self.unchanged += 1
                return r.status_code, None, False
            self.fetched += 1
            return r.status_code, body, True
        return None, None, False


class Geocoder:
    """Hardcoded allowlist handles the common case in filters.py; this is the
    Nominatim fallback for unrecognised names. 1 req/sec, descriptive UA,
    every resolution cached permanently in state."""

    def __init__(self, fetcher: Fetcher, state: State):
        self.fetcher = fetcher
        self.cache = state.data["geocode_cache"]

    def __call__(self, place: str) -> Optional[str]:
        key = normalise(place)[:120]
        if not key:
            return None
        if key in self.cache:
            return self.cache[key].get("municipality")
        if self.fetcher.out_of_time():
            return None
        self.fetcher._throttle("https://nominatim.openstreetmap.org/")
        try:
            r = self.fetcher.session.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": place, "format": "jsonv2", "limit": 1,
                        "addressdetails": 1},
                timeout=10)
            data = r.json() if r.status_code == 200 else []
        except (requests.RequestException, ValueError):
            data = []
        muni, country = None, None
        if data:
            addr = data[0].get("address", {})
            muni = (addr.get("municipality") or addr.get("city")
                    or addr.get("town") or addr.get("county"))
            country = addr.get("country_code")
        self.cache[key] = {"municipality": muni, "country": country}
        return muni
