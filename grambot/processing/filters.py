"""Keyword-based relevance filtering."""
from __future__ import annotations

import re
from typing import Iterable, List

from ..sources import NewsItem

# Short all-caps keywords are almost always ticker/acronym mentions (e.g.
# "TON", "GRAM"), but they also collide with common English words used as
# units or names ("50-ton truck", "1 gram of salt", "Gram Parsons"). For
# these we require the source text to use the same all-caps spelling, so
# "TON" matches "TON Foundation" but not "50-ton" or "Ton Parsons".
_AMBIGUOUS_MAX_LEN = 5


def _keyword_pattern(keyword: str) -> re.Pattern:
    """Build a whole-word regex for a keyword.

    Word boundaries keep matches to the actual token (not substrings inside
    unrelated words like "Tonawanda" or "Stonehenge"). Short, fully
    upper-case keywords are matched case-sensitively to avoid colliding with
    common lowercase/capitalized English words (see module docstring).
    """
    stripped = keyword.strip()
    escaped = re.escape(stripped)
    is_ambiguous_short_ticker = stripped.isalpha() and stripped.isupper() and len(stripped) <= _AMBIGUOUS_MAX_LEN
    flags = 0 if is_ambiguous_short_ticker else re.IGNORECASE
    return re.compile(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", flags)


def is_relevant(item: NewsItem, keywords: Iterable[str]) -> bool:
    """Return True if the item's text mentions any of the given keywords.

    See `_keyword_pattern` for the whole-word / case-sensitivity rules. This
    is intentionally simple and cheap so it can run on every item before the
    more expensive classification step.
    """
    text = item.text
    return any(_keyword_pattern(keyword).search(text) for keyword in keywords if keyword.strip())


def filter_relevant(items: Iterable[NewsItem], keywords: Iterable[str]) -> List[NewsItem]:
    keywords = list(keywords)
    return [item for item in items if is_relevant(item, keywords)]
