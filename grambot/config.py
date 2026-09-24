"""Configuration loading for the GRAM/TON monitor bot."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

try:  # pragma: no cover - exercised indirectly
    from dotenv import load_dotenv

    # Load the project's .env explicitly so the bot finds it regardless of the
    # working directory it was launched from (launchd, cron, IDE), then fall
    # back to the cwd. Already-set environment variables always take priority.
    load_dotenv(PROJECT_ROOT / ".env")
    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is an optional convenience
    pass


def _split_csv(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _env_list(name: str, default: List[str]) -> List[str]:
    raw = os.getenv(name)
    return _split_csv(raw) if raw else list(default)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid integer for %s=%r, using default %s", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        logger.warning("Invalid number for %s=%r, using default %s", name, raw, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_currency(name: str, default: str) -> str:
    raw = (os.getenv(name) or "").strip().upper()
    if len(raw) == 3 and raw.isalpha():
        return raw
    if raw:
        logger.warning("%s=%r is not a 3-letter currency code; using %s", name, raw, default)
    return default


DEFAULT_RSS_FEEDS = [
    "https://news.google.com/rss/search?q=TON+OR+Toncoin+OR+GRAM+OR+%22The+Open+Network%22&hl=en-US&gl=US&ceid=US:en",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://cryptoslate.com/feed/",
    "https://www.theblock.co/rss.xml",
    # Telegram channels via a public RSSHub instance. Public bridges can be
    # rate-limited or go offline; self-host RSSHub (see README) for
    # reliability and replace the base URL below with your own instance.
    "https://rsshub.rssforever.com/telegram/channel/tonblockchain",
    "https://rsshub.rssforever.com/telegram/channel/tonstatus",
    "https://rsshub.rssforever.com/telegram/channel/durov",
    "https://rsshub.rssforever.com/telegram/channel/telegram",
    # Official node releases (network upgrades). Not trusted for alerts on
    # their own: changelog wording ("fixed crash") confuses sentiment.
    "https://github.com/ton-blockchain/ton/releases.atom",
]

DEFAULT_KEYWORDS = [
    "TON",
    "Toncoin",
    "GRAM",
    "Telegram",
    "The Open Network",
    "TON Foundation",
    "Durov",
]

# Items whose source name contains one of these (case-insensitive) are treated
# as first-party/official and count as verified on their own, without waiting
# for a second outlet to corroborate them.
DEFAULT_TRUSTED_SOURCES = [
    "The Open Network - Telegram Channel",
    "TON Status - Telegram Channel",
    "Pavel Durov - Telegram Channel",
    "Telegram News - Telegram Channel",
]


@dataclass
class Settings:
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    rss_feeds: List[str] = field(default_factory=lambda: list(DEFAULT_RSS_FEEDS))
    keywords: List[str] = field(default_factory=lambda: list(DEFAULT_KEYWORDS))
    trusted_sources: List[str] = field(default_factory=lambda: list(DEFAULT_TRUSTED_SOURCES))
    poll_interval_seconds: int = 300
    price_poll_interval_seconds: int = 120
    feed_timeout_seconds: int = 15
    max_item_age_hours: int = 24
    min_notify_strength: str = "low"
    cluster_window_minutes: int = 120
    min_sources_for_verified: int = 2
    max_notifications_per_hour: int = 10
    price_window_minutes: int = 20
    price_alert_threshold_pct: float = 5.0
    price_alert_cooldown_minutes: int = 60
    retention_days: int = 30
    enable_commands: bool = True
    send_startup_message: bool = True
    coingecko_coin_id: str = "the-open-network"
    price_symbol: str = "GRAMUSDT"
    display_currency: str = "USD"
    database_path: str = "grambot.db"
    enable_onchain: bool = True
    toncenter_api_key: str = ""
    onchain_poll_interval_seconds: int = 120
    whale_min_ton: float = 500_000.0
    onchain_max_pages: int = 8
    onchain_alert_cooldown_minutes: int = 10
    network_stall_minutes: int = 5
    labels_url: str = "https://raw.githubusercontent.com/shuva10v/ton-labels/build/assets.json"
    max_memory_mb: float = 512.0
    watchdog_timeout_minutes: int = 20
    health_alert_minutes: int = 60
    heartbeat_path: str = ""
    log_file: str = ""
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"

    @property
    def heartbeat_file(self) -> str:
        return self.heartbeat_path or f"{self.database_path}.heartbeat"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            rss_feeds=_env_list("RSS_FEEDS", DEFAULT_RSS_FEEDS),
            keywords=_env_list("KEYWORDS", DEFAULT_KEYWORDS),
            trusted_sources=_env_list("TRUSTED_SOURCES", DEFAULT_TRUSTED_SOURCES),
            poll_interval_seconds=max(30, _env_int("POLL_INTERVAL_SECONDS", 300)),
            price_poll_interval_seconds=max(30, _env_int("PRICE_POLL_INTERVAL_SECONDS", 120)),
            feed_timeout_seconds=max(3, _env_int("FEED_TIMEOUT_SECONDS", 15)),
            max_item_age_hours=max(1, _env_int("MAX_ITEM_AGE_HOURS", 24)),
            min_notify_strength=os.getenv("MIN_NOTIFY_STRENGTH", "low").strip().lower() or "low",
            cluster_window_minutes=max(1, _env_int("CLUSTER_WINDOW_MINUTES", 120)),
            min_sources_for_verified=max(1, _env_int("MIN_SOURCES_FOR_VERIFIED", 2)),
            max_notifications_per_hour=max(1, _env_int("MAX_NOTIFICATIONS_PER_HOUR", 10)),
            price_window_minutes=max(1, _env_int("PRICE_WINDOW_MINUTES", 20)),
            price_alert_threshold_pct=max(0.1, _env_float("PRICE_ALERT_THRESHOLD_PCT", 5.0)),
            price_alert_cooldown_minutes=max(0, _env_int("PRICE_ALERT_COOLDOWN_MINUTES", 60)),
            retention_days=max(1, _env_int("RETENTION_DAYS", 30)),
            enable_commands=_env_bool("ENABLE_COMMANDS", True),
            send_startup_message=_env_bool("SEND_STARTUP_MESSAGE", True),
            coingecko_coin_id=os.getenv("COINGECKO_COIN_ID", "the-open-network").strip() or "the-open-network",
            price_symbol=os.getenv("PRICE_SYMBOL", "GRAMUSDT").strip().upper() or "GRAMUSDT",
            display_currency=_env_currency("DISPLAY_CURRENCY", "USD"),
            database_path=os.getenv("DATABASE_PATH", "grambot.db").strip() or "grambot.db",
            enable_onchain=_env_bool("ENABLE_ONCHAIN", True),
            toncenter_api_key=os.getenv("TONCENTER_API_KEY", "").strip(),
            onchain_poll_interval_seconds=max(30, _env_int("ONCHAIN_POLL_INTERVAL_SECONDS", 120)),
            whale_min_ton=max(1000.0, _env_float("WHALE_MIN_TON", 500_000.0)),
            onchain_max_pages=max(1, _env_int("ONCHAIN_MAX_PAGES", 8)),
            onchain_alert_cooldown_minutes=max(0, _env_int("ONCHAIN_ALERT_COOLDOWN_MINUTES", 10)),
            network_stall_minutes=max(1, _env_int("NETWORK_STALL_MINUTES", 5)),
            labels_url=os.getenv("LABELS_URL", cls.labels_url).strip(),
            max_memory_mb=max(0.0, _env_float("MAX_MEMORY_MB", 512.0)),
            watchdog_timeout_minutes=max(0, _env_int("WATCHDOG_TIMEOUT_MINUTES", 20)),
            health_alert_minutes=max(1, _env_int("HEALTH_ALERT_MINUTES", 60)),
            heartbeat_path=os.getenv("HEARTBEAT_PATH", "").strip(),
            log_file=os.getenv("LOG_FILE", "").strip(),
            openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip(),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini",
        )
