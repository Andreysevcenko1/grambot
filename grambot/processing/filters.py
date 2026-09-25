"""Keyword and freshness filtering of raw news items."""
from __future__ import annotations

import time
from typing import Iterable, List, Optional

from ..sources import NewsItem
from .keywords import matches_any


def is_relevant(item: NewsItem, keywords: Iterable[str]) -> bool:
    return bool(matches_any(item.text, keywords))


def filter_relevant(items: Iterable[NewsItem], keywords: Iterable[str]) -> List[NewsItem]:
    kws = [kw for kw in keywords if kw.strip()]
    return [item for item in items if is_relevant(item, kws)]


def filter_fresh(
    items: Iterable[NewsItem], max_age_hours: float, now: Optional[float] = None
) -> List[NewsItem]:
    """Drop items older than ``max_age_hours`` (feeds carry weeks of backlog)."""
    reference = now if now is not None else time.time()
    cutoff = reference - max_age_hours * 3600
    return [item for item in items if item.published_at >= cutoff]
