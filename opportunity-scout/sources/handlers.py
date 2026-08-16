"""Generic source handlers. sources.yaml decides what exists; this module only
knows the three shapes: page, rss, tavily. One dead source never kills a run —
main.py wraps each in try/except and logs it into diagnostics.

Funnel per source:
  listing fetch → JSON-LD on the listing → harvested links → keyword triage
  (zero cost) → conditional fetch of survivors → JSON-LD on details → pages
  with no structured data become LLM candidates for the shared batch queue.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

import filters
from extract import parse_jsonld, snippet_event
from models import Event
from sources.base import Fetcher

SOCIAL_HOSTS = ("linkedin.com", "facebook.com", "instagram.com", "x.com",
                "twitter.com")


@dataclass
class SourceResult:
    name: str
    ok: bool = True
    error: str = ""
    events: List[Event] = field(default_factory=list)
    llm_candidates: List[dict] = field(default_factory=list)


def run_source(src: dict, fetcher: Fetcher, cfg: dict,
               tavily_key: str, tavily_budget) -> SourceResult:
    res = SourceResult(name=src["name"])
    kind = src.get("type", "page")
    if kind == "tavily":
        _tavily(src, cfg, tavily_key, tavily_budget, res)
    elif kind == "rss":
        _rss(src, fetcher, cfg, res)
    else:
        _page(src, fetcher, cfg, res)
    return res


# ---------------------------------------------------------------- page/rss
def _detail_cap(src: dict, cfg: dict) -> int:
    return int(src.get("max_detail", cfg["settings"]["max_detail_pages_per_source"]))


def _note_fetch_failure(res: SourceResult, url: str, status) -> None:
    label = ("network error" if status is None
             else "robots.txt disallows" if status == 999
             else f"HTTP {status}")
    res.error = (res.error + "; " if res.error else "") + f"{label} on {url}"
    res.ok = False


def _page(src: dict, fetcher: Fetcher, cfg: dict, res: SourceResult) -> None:
    pattern = src.get("link_pattern")
    budget = _detail_cap(src, cfg)
    for url in src.get("urls", []):
        if fetcher.out_of_time():
            return
        status, body, changed = fetcher.get(url)
        if not changed:
            if status is None or (status != 304 and status >= 400):
                _note_fetch_failure(res, url, status)
            continue
        res.events += parse_jsonld(body, url, src["name"], status)
        soup = BeautifulSoup(body, "html.parser")
        for a in soup.find_all("a", href=True):
            if budget <= 0 or fetcher.out_of_time():
                return
            href = urljoin(url, a["href"])
            if urlparse(href).netloc == "" or href.rstrip("/") == url.rstrip("/"):
                continue
            if pattern and not re.search(pattern, href):
                continue
            text = a.get_text(" ", strip=True)
            if not filters.triage(text, href, "", cfg):
                continue
            budget -= 1
            _detail(href, src["name"], fetcher, res)


def _rss(src: dict, fetcher: Fetcher, cfg: dict, res: SourceResult) -> None:
    budget = _detail_cap(src, cfg)
    for url in src.get("urls", []):
        status, body, changed = fetcher.get(url)
        if not changed:
            if status is None or (status != 304 and status >= 400):
                _note_fetch_failure(res, url, status)
            continue
        feed = feedparser.parse(body)
        for entry in feed.entries:
            if budget <= 0 or fetcher.out_of_time():
                return
            title = entry.get("title", "")
            link = entry.get("link", "")
            summary = re.sub(r"<[^>]+>", " ", entry.get("summary", ""))
            if not link or not filters.triage(title, link, summary, cfg):
                continue
            budget -= 1
            _detail(link, src["name"], fetcher, res)


def _detail(url: str, source: str, fetcher: Fetcher, res: SourceResult) -> None:
    status, body, changed = fetcher.get(url)
    if not changed or not body:
        return
    found = parse_jsonld(body, url, source, status)
    if found:
        res.events += found
        return
    text = BeautifulSoup(body, "html.parser").get_text(" ", strip=True)
    if text:
        res.llm_candidates.append(
            {"url": url, "source": source, "http_status": status, "text": text})


# -------------------------------------------------------------------- tavily
def _tavily(src: dict, cfg: dict, api_key: str, budget, res: SourceResult) -> None:
    """budget is the shared TavilyBudget from main.py — hard cap 30/day,
    persisted in state. When exhausted, skip and note it in diagnostics."""
    if not api_key:
        res.ok, res.error = False, "TAVILY_API_KEY not set"
        return
    for query in src.get("queries", []):
        if not budget.take():
            res.error = "daily Tavily cap reached; remaining queries skipped"
            return
        try:
            r = requests.post("https://api.tavily.com/search",
                              json={"api_key": api_key, "query": query,
                                    "search_depth": "basic",
                                    "max_results": int(src.get("max_results", 4))},
                              timeout=25)
            r.raise_for_status()
            results = r.json().get("results", [])
        except (requests.RequestException, ValueError) as e:
            res.ok, res.error = False, f"{type(e).__name__}: {e}"[:150]
            return
        for item in results:
            title = item.get("title", "")
            url = item.get("url", "")
            snippet = item.get("content", "")
            if not url or not filters.triage(title, url, snippet, cfg):
                continue
            host = urlparse(url).netloc.replace("www.", "")
            if any(host.endswith(s) for s in SOCIAL_HOSTS):
                # LinkedIn/Facebook block direct fetching (and we never
                # authenticate against social platforms) → snippet-only,
                # quarantined in the Low confidence section.
                res.events.append(snippet_event(title, url, snippet, src["name"]))
            else:
                res.llm_candidates.append(
                    {"url": url, "source": src["name"], "http_status": None,
                     "text": f"{title}\n{snippet}", "needs_fetch": True})
