"""Normalize public exchange announcements and identify spot listings."""
import datetime
import logging
import re
import time
from typing import Optional

import requests

from . import NewsItem
from .rss import FeedResult, USER_AGENT

logger = logging.getLogger(__name__)
_TICKER = re.compile(r"\(([A-Z0-9]{2,12})\)")
_PAIR = re.compile(r"\b([A-Z0-9]{2,12})/(?:USDT|USDC|USD|EUR|BTC)\b")
_BINANCE = re.compile(r"\bBinance (?:will list|lists|to list)\b", re.I)
_OKX = re.compile(r"\bOKX (?:to list|will list|will launch)\b", re.I)


def listing_label(title: str, exchange: str) -> Optional[str]:
    if exchange == "Upbit":
        if not ("신규 거래지원" in title or "마켓 디지털 자산 추가" in title):
            return None
    elif exchange == "Binance":
        if not _BINANCE.search(title):
            return None
    elif exchange == "OKX":
        if not _OKX.search(title) or "spot" not in title.lower():
            return None
    else:
        return None
    match = _TICKER.search(title) or _PAIR.search(title)
    if match:
        return match.group(1)
    # Keep a listing even when its ticker is ambiguous; never guess one.
    return "тикер уточняется"


def fetch_listings(url: str, exchange: str, timeout: float, session=None) -> FeedResult:
    started = time.time()
    try:
        response = (session or requests).get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        payload = response.json()
        if exchange == "Upbit":
            notices = payload["data"]["notices"]
        else:
            if payload.get("code") != "0":
                raise ValueError("OKX API error: " + str(payload.get("msg", payload.get("code"))))
            notices = payload["data"][0]["details"]
        items = []
        for notice in notices:
            title = notice["title"]
            if exchange == "Upbit":
                published = datetime.datetime.fromisoformat(notice["first_listed_at"].replace("Z", "+00:00")).timestamp()
                link = "https://upbit.com/service_center/notice?id=" + str(notice["id"])
            else:
                published = int(notice["pTime"]) / 1000
                link = notice["url"]
            items.append(NewsItem(exchange, title, "", link, published))
        return FeedResult(url=url, ok=True, items=items, feed_title=exchange,
                          fetched_at=started, duration_seconds=time.time() - started)
    except (requests.RequestException, ValueError, KeyError, TypeError, IndexError) as exc:
        logger.warning("%s announcements failed: %s", exchange, exc)
        return FeedResult(url=url, ok=False, error=str(exc), feed_title=exchange,
                          fetched_at=started, duration_seconds=time.time() - started)
