"""Configuration loading for the GRAM/TON monitor bot."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

try:  # pragma: no cover - exercised indirectly
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is an optional convenience
    pass


def _split_csv(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


DEFAULT_RSS_FEEDS = [
    "https://blog.ton.org/rss",
    "https://telegram.org/blog/rss",
]

DEFAULT_KEYWORDS = [
    "TON",
    "Toncoin",
    "GRAM",
    "Telegram",
    "The Open Network",
    "TON Foundation",
]


@dataclass
class Settings:
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    rss_feeds: List[str] = field(default_factory=lambda: list(DEFAULT_RSS_FEEDS))
    keywords: List[str] = field(default_factory=lambda: list(DEFAULT_KEYWORDS))
    poll_interval_seconds: int = 300
    price_poll_interval_seconds: int = 120
    min_notify_strength: str = "low"
    cluster_window_minutes: int = 120
    min_sources_for_verified: int = 2
    coingecko_coin_id: str = "the-open-network"
    database_path: str = "grambot.db"

    @classmethod
    def from_env(cls) -> "Settings":
        rss_feeds_env = os.getenv("RSS_FEEDS")
        keywords_env = os.getenv("KEYWORDS")
        return cls(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            rss_feeds=_split_csv(rss_feeds_env) if rss_feeds_env else list(DEFAULT_RSS_FEEDS),
            keywords=_split_csv(keywords_env) if keywords_env else list(DEFAULT_KEYWORDS),
            poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "300")),
            price_poll_interval_seconds=int(
                os.getenv("PRICE_POLL_INTERVAL_SECONDS", "120")
            ),
            min_notify_strength=os.getenv("MIN_NOTIFY_STRENGTH", "low"),
            cluster_window_minutes=int(os.getenv("CLUSTER_WINDOW_MINUTES", "120")),
            min_sources_for_verified=int(os.getenv("MIN_SOURCES_FOR_VERIFIED", "2")),
            coingecko_coin_id=os.getenv("COINGECKO_COIN_ID", "the-open-network"),
            database_path=os.getenv("DATABASE_PATH", "grambot.db"),
        )
