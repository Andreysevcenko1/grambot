"""Shared keyword matching helpers.

Keywords are matched on word boundaries so "TON" does not fire on "Ken-Ton",
"ton of gold" style false positives are avoided as much as possible, and
"hack" does not match "hackathon". Short all-caps tickers (TON, GRAM, SEC,
ETF...) are matched case-sensitively; everything else is case-insensitive.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable, List

# Word characters plus a few symbols that commonly glue to tickers in
# headlines (e.g. "$TON", "TON/USDT").
_BOUNDARY_LEFT = r"(?<![\w-])"
_BOUNDARY_RIGHT = r"(?![\w-])"


@lru_cache(maxsize=1024)
def compile_keyword(keyword: str) -> "re.Pattern[str]":
    """Compile a keyword into a boundary-aware regex.

    A trailing ``*`` turns the keyword into a prefix match ("взлом*" matches
    "взломан", "взломали"), which is the cheap way to cope with inflected
    languages such as Russian.
    """
    keyword = keyword.strip()
    prefix_match = keyword.endswith("*")
    if prefix_match:
        keyword = keyword[:-1].rstrip()
    escaped = r"\s+".join(re.escape(part) for part in keyword.split())
    right = r"\w*" + _BOUNDARY_RIGHT if prefix_match else _BOUNDARY_RIGHT
    pattern = f"{_BOUNDARY_LEFT}{escaped}{right}"
    # Short uppercase tokens are tickers/acronyms: keep them case-sensitive,
    # otherwise "ton" (unit of weight) and "gram" (unit of mass) drown the feed.
    if keyword.isupper() and len(keyword) <= 5:
        return re.compile(pattern)
    return re.compile(pattern, re.IGNORECASE)


def matches_any(text: str, keywords: Iterable[str]) -> List[str]:
    """Return the keywords (in the given order) that occur in ``text``."""
    return [kw for kw in keywords if kw.strip() and compile_keyword(kw).search(text)]
