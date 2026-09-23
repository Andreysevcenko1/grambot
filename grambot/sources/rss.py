"""RSS/Atom feed source.

This also covers Telegram channels and X/Twitter accounts as long as they
are exposed through an RSS bridge (e.g. RSSHub, Nitter, or a channel's own
RSS export), which keeps collection compliant with each platform's API
terms instead of scraping them directly.
"""
from __future__ import annotations

import calendar
import logging
import time
from typing import List

import feedparser

from . import NewsItem

logger = logging.getLogger(__name__)


def _entry_timestamp(entry) -> float:
    for key in ("published_parsed", "updated_parsed"):
        value = getattr(entry, key, None)
        if value:
            return calendar.timegm(value)
    return time.time()


def fetch_rss_items(feed_url: str, source_name: str | None = None) -> List[NewsItem]:
    """Fetch and normalize entries from a single RSS/Atom feed URL."""
    source = source_name or feed_url
    try:
        parsed = feedparser.parse(feed_url)
    except Exception:  # pragma: no cover - defensive, network dependent
        logger.exception("Failed to fetch feed %s", feed_url)
        return []

    if getattr(parsed, "bozo", False) and not parsed.entries:
        logger.warning("Feed %s could not be parsed: %s", feed_url, parsed.get("bozo_exception"))

    items: List[NewsItem] = []
    for entry in parsed.entries:
        title = getattr(entry, "title", "").strip()
        if not title:
            continue
        summary = getattr(entry, "summary", "") or ""
        url = getattr(entry, "link", None)
        items.append(
            NewsItem(
                source=source,
                title=title,
                summary=summary,
                url=url,
                published_at=_entry_timestamp(entry),
            )
        )
    return items


def fetch_all(feed_urls: List[str]) -> List[NewsItem]:
    items: List[NewsItem] = []
    for feed_url in feed_urls:
        items.extend(fetch_rss_items(feed_url))
    return items
