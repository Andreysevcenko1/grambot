"""RSS/Atom collection.

Feeds are fetched with ``requests`` (explicit timeout, browser-like User-Agent,
parallel) and then handed to ``feedparser`` for parsing. Publisher names are
extracted so that aggregator feeds such as Google News count each outlet as a
separate source when verifying a story.
"""
from __future__ import annotations

import calendar
import concurrent.futures
import html
import logging
import re
import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence
from urllib.parse import urlparse

import feedparser
import requests

from . import NewsItem

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; grambot/0.2; +https://github.com/Andreysevcenko1/grambot)"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# Google News: "Headline - Publisher"; other aggregators use "|" or "—".
_TITLE_SUFFIX_RE = re.compile(r"\s+[-|–—]\s+([^-|–—]{2,60})$")


@dataclass
class FeedResult:
    url: str
    ok: bool
    items: List[NewsItem] = field(default_factory=list)
    error: Optional[str] = None
    feed_title: str = ""
    duration_seconds: float = 0.0
    fetched_at: float = 0.0


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _entry_timestamp(entry, default: float) -> float:
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(attr)
        if parsed:
            try:
                return float(calendar.timegm(parsed))
            except (TypeError, ValueError, OverflowError):
                continue
    return default


def _domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _split_publisher_suffix(title: str) -> "tuple[str, Optional[str]]":
    match = _TITLE_SUFFIX_RE.search(title)
    if not match:
        return title, None
    return title[: match.start()].strip(), match.group(1).strip()


def _source_name(entry, feed_title: str, feed_url: str) -> "tuple[str, str]":
    """Return ``(source_name, cleaned_title)`` for an entry."""
    title = strip_html(entry.get("title", ""))
    publisher = None
    source = entry.get("source")
    if isinstance(source, dict):
        publisher = strip_html(source.get("title", "")) or None
    if publisher:
        base, suffix = _split_publisher_suffix(title)
        if suffix and suffix.lower() == publisher.lower():
            title = base
        return publisher, title
    if feed_title:
        return feed_title, title
    return _domain(feed_url) or feed_url, title


def parse_feed(content: bytes, feed_url: str, fetched_at: Optional[float] = None) -> FeedResult:
    now = fetched_at if fetched_at is not None else time.time()
    parsed = feedparser.parse(content)
    feed_title = strip_html(parsed.feed.get("title", "")) if parsed.get("feed") else ""
    entries = parsed.get("entries", [])
    if parsed.get("bozo") and not entries:
        return FeedResult(
            url=feed_url,
            ok=False,
            error=f"could not be parsed: {parsed.get('bozo_exception')}",
            feed_title=feed_title,
            fetched_at=now,
        )

    items: List[NewsItem] = []
    for entry in entries:
        source, title = _source_name(entry, feed_title, feed_url)
        if not title:
            continue
        summary = strip_html(entry.get("summary", "") or entry.get("description", ""))
        items.append(
            NewsItem(
                source=source,
                title=title,
                summary=summary[:1000],
                url=entry.get("link"),
                published_at=_entry_timestamp(entry, now),
            )
        )
    return FeedResult(url=feed_url, ok=True, items=items, feed_title=feed_title, fetched_at=now)


def fetch_feed(feed_url: str, timeout: float = 15.0, session: Optional[requests.Session] = None) -> FeedResult:
    started = time.time()
    http = session or requests
    try:
        response = http.get(
            feed_url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"},
        )
        response.raise_for_status()
        result = parse_feed(response.content, feed_url, fetched_at=started)
    except requests.RequestException as exc:
        result = FeedResult(url=feed_url, ok=False, error=str(exc), fetched_at=started)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Unexpected error while fetching %s", feed_url)
        result = FeedResult(url=feed_url, ok=False, error=repr(exc), fetched_at=started)
    result.duration_seconds = time.time() - started
    if not result.ok:
        logger.warning("Feed %s failed: %s", feed_url, result.error)
    return result


class RSSSource:
    """Collects items from a list of feeds, fetching them in parallel."""

    def __init__(self, feed_urls: Sequence[str], timeout: float = 15.0, max_workers: int = 6):
        self.feed_urls = list(feed_urls)
        self.timeout = timeout
        self.max_workers = max_workers
        self.last_results: List[FeedResult] = []

    def fetch_all(self) -> List[FeedResult]:
        if not self.feed_urls:
            return []
        workers = max(1, min(self.max_workers, len(self.feed_urls)))
        with requests.Session() as session, concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fetch_feed, url, self.timeout, session): url for url in self.feed_urls}
            results = [future.result() for future in futures]
        # Keep the configured order for status output.
        order = {url: idx for idx, url in enumerate(self.feed_urls)}
        results.sort(key=lambda r: order.get(r.url, 0))
        self.last_results = results
        return results

    def fetch(self) -> List[NewsItem]:
        items: List[NewsItem] = []
        for result in self.fetch_all():
            items.extend(result.items)
        return items
