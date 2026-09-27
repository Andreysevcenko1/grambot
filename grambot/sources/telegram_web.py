"""Telegram channel posts via the public web preview (``https://t.me/s/<channel>``).

This is exactly what RSSHub's ``/telegram/channel`` route scrapes, minus the
Node.js service: the preview page lists the ~20 latest posts of any public
channel that has web previews enabled. Posts become ``NewsItem`` objects with
the source ``"<Channel title> - Telegram Channel"`` (RSSHub-compatible, so
``TRUSTED_SOURCES`` entries keep working) and the permalink ``t.me/<channel>/<id>``.
"""
from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime
from typing import List, Optional

import requests

from . import NewsItem
from .rss import FeedResult, USER_AGENT

logger = logging.getLogger(__name__)

PREVIEW_URL = "https://t.me/s/{channel}"
POST_URL = "https://t.me/{channel}/{post_id}"
SOURCE_SUFFIX = " - Telegram Channel"
MAX_TITLE_LENGTH = 200

_CHANNEL_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:s/)?(?P<name>[A-Za-z][A-Za-z0-9_]{3,63})/?(?:\?.*)?$"
)
_CHANNEL_SHORT_RE = re.compile(r"^(?:@|telegram:|tg://)(?P<name>[A-Za-z][A-Za-z0-9_]{3,63})$")
_MESSAGE_START_RE = re.compile(r'<div class="tgme_widget_message[^"]*js-widget_message"[^>]*data-post="([^"/]+)/(\d+)"')
_TEXT_RE = re.compile(r'<div class="tgme_widget_message_text js-message_text"[^>]*>(.*?)</div>', re.S)
_TIME_RE = re.compile(r'<time datetime="([^"]+)"')
_OG_TITLE_RE = re.compile(r'<meta property="og:title" content="([^"]*)"')
_PAGE_TITLE_RE = re.compile(r'<div class="tgme_channel_info_header_title"[^>]*><span[^>]*>(.*?)</span>', re.S)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACES_RE = re.compile(r"[ \t\u00a0]+")


def channel_from_url(url: str) -> Optional[str]:
    """Channel username for ``t.me/s/<name>``, ``t.me/<name>``, ``@name``, ``telegram:name``."""
    text = (url or "").strip()
    match = _CHANNEL_URL_RE.match(text) or _CHANNEL_SHORT_RE.match(text)
    if not match:
        return None
    name = match.group("name")
    if name.lower() in {"s", "joinchat", "share", "addstickers", "proxy", "socks", "iv", "setlanguage"}:
        return None
    return name


_POST_URL_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:s/)?(?P<name>[A-Za-z][A-Za-z0-9_]{3,63})(?:/\d+)?/?(?:\?.*)?$")


def channel_from_post_url(url: str) -> Optional[str]:
    """Channel username for a post permalink such as ``https://t.me/durov/123``."""
    match = _POST_URL_RE.match((url or "").strip())
    if not match:
        return None
    name = match.group("name")
    return None if name.lower() == "s" else name


def _to_text(fragment: str) -> str:
    text = _BR_RE.sub("\n", fragment)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    lines = [_SPACES_RE.sub(" ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def _parse_time(value: str, default: float) -> float:
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return default
    if stamp.tzinfo is None:
        return default
    return stamp.timestamp()


def _channel_title(page: str, channel: str) -> str:
    match = _PAGE_TITLE_RE.search(page) or _OG_TITLE_RE.search(page)
    title = _to_text(match.group(1)) if match else ""
    title = title.replace("\n", " ").strip()
    if title.lower().startswith("telegram: contact"):
        title = ""
    return title or channel


def parse_channel_page(page: str, channel: str, feed_url: str, fetched_at: Optional[float] = None) -> FeedResult:
    now = fetched_at if fetched_at is not None else time.time()
    title = _channel_title(page, channel)
    source = f"{title}{SOURCE_SUFFIX}"
    starts = list(_MESSAGE_START_RE.finditer(page))
    if not starts:
        if "tgme_page_title" in page or "tgme_page_description" in page or "tgme_channel_info" not in page:
            error = "preview unavailable (private channel, restricted content or unknown username)"
        else:
            error = "no posts found on the preview page"
        return FeedResult(url=feed_url, ok=False, error=error, feed_title=source, fetched_at=now)

    actual = starts[0].group(1)
    if actual.lower() != channel.lower():
        # The channel was renamed; ``channel`` is an alias. Trust rules should list both.
        logger.info("Telegram channel @%s now posts as @%s", channel, actual)

    items: List[NewsItem] = []
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(page)
        block = page[match.start():end]
        text_match = _TEXT_RE.search(block)
        if not text_match:
            continue  # media without a caption, polls, service messages
        text = _to_text(text_match.group(1))
        if not text:
            continue
        first_line = text.split("\n", 1)[0]
        headline = first_line if len(first_line) <= MAX_TITLE_LENGTH else first_line[: MAX_TITLE_LENGTH - 1].rstrip() + "…"
        time_match = _TIME_RE.search(block)
        published = _parse_time(time_match.group(1), now) if time_match else now
        post_channel, post_id = match.group(1), match.group(2)
        items.append(
            NewsItem(
                source=source,
                title=headline,
                summary=text.replace("\n", " ")[:1000],
                url=POST_URL.format(channel=post_channel, post_id=post_id),
                published_at=published,
            )
        )
    return FeedResult(url=feed_url, ok=True, items=items, feed_title=source, fetched_at=now)


def fetch_channel(
    channel: str,
    feed_url: str,
    timeout: float = 15.0,
    session: Optional[requests.Session] = None,
) -> FeedResult:
    started = time.time()
    http = session or requests
    try:
        response = http.get(
            PREVIEW_URL.format(channel=channel),
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*", "Accept-Language": "en"},
        )
        response.raise_for_status()
        if "/s/" not in response.url:
            # t.me redirects to the plain profile page when the preview is disabled.
            result = FeedResult(
                url=feed_url, ok=False, error="preview disabled or channel not found", feed_title=channel, fetched_at=started
            )
        else:
            result = parse_channel_page(response.text, channel, feed_url, fetched_at=started)
    except requests.RequestException as exc:
        result = FeedResult(url=feed_url, ok=False, error=str(exc), fetched_at=started)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Unexpected error while fetching Telegram channel %s", channel)
        result = FeedResult(url=feed_url, ok=False, error=repr(exc), fetched_at=started)
    result.duration_seconds = time.time() - started
    if not result.ok:
        logger.warning("Telegram channel @%s failed: %s", channel, result.error)
    return result

