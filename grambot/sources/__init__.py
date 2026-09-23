"""Data model shared across sources."""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class NewsItem:
    """A single normalized news item collected from any source."""

    source: str
    title: str
    summary: str
    url: Optional[str]
    published_at: float

    @property
    def item_hash(self) -> str:
        """Stable identifier used for dedup, based on source + url/title."""
        basis = f"{self.source}|{self.url or self.title}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.summary}".strip()


def now() -> float:
    return time.time()
