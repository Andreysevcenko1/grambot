"""Keyword-based relevance filtering."""
from __future__ import annotations

from typing import Iterable, List

from ..sources import NewsItem


def is_relevant(item: NewsItem, keywords: Iterable[str]) -> bool:
    """Return True if the item's text mentions any of the given keywords.

    Matching is case-insensitive and looks for whole keyword substrings,
    which is intentionally simple and cheap so it can run on every item
    before the more expensive classification step.
    """
    text = item.text.lower()
    return any(keyword.lower() in text for keyword in keywords if keyword)


def filter_relevant(items: Iterable[NewsItem], keywords: Iterable[str]) -> List[NewsItem]:
    keywords = list(keywords)
    return [item for item in items if is_relevant(item, keywords)]
