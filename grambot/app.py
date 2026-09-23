"""Main polling loop that wires sources, filtering, classification,
clustering, price checks and notifications together.
"""
from __future__ import annotations

import logging
import time
from typing import List

from .config import Settings
from .notifier import TelegramNotifier, format_message
from .price import poll_and_store, recent_move
from .processing.classifier import RuleBasedClassifier, meets_min_strength
from .processing.clustering import find_matching_cluster, new_cluster_id
from .processing.filters import filter_relevant
from .sources import NewsItem
from .sources.rss import fetch_all
from .storage import Storage

logger = logging.getLogger(__name__)


class GramTonMonitor:
    """Ties together collection, processing and notification for one poll cycle."""

    def __init__(self, settings: Settings, storage: Storage, notifier: TelegramNotifier):
        self.settings = settings
        self.storage = storage
        self.notifier = notifier
        self.classifier = RuleBasedClassifier()

    def poll_price(self) -> None:
        poll_and_store(self.storage, self.settings.coingecko_coin_id)

    def poll_news_once(self) -> int:
        """Fetch, process and (maybe) notify. Returns the number of new items processed."""
        raw_items = fetch_all(self.settings.rss_feeds)
        relevant_items = filter_relevant(raw_items, self.settings.keywords)

        new_items = [item for item in relevant_items if not self.storage.has_seen(item.item_hash)]
        for item in new_items:
            self._process_item(item)
        return len(new_items)

    def _process_item(self, item: NewsItem) -> None:
        window_seconds = self.settings.cluster_window_minutes * 60
        since_ts = time.time() - window_seconds
        candidates = [
            (cluster.cluster_id, cluster.representative_title)
            for cluster in self.storage.recent_clusters(since_ts)
        ]
        cluster_id = find_matching_cluster(item.title, candidates)
        if cluster_id is None:
            bucket = str(int(item.published_at // window_seconds)) if window_seconds else "0"
            cluster_id = new_cluster_id(item.title, bucket)

        self.storage.upsert_cluster(cluster_id, item.title, item.published_at)
        self.storage.mark_seen(
            item.item_hash, item.source, item.title, item.url, item.published_at, cluster_id
        )

        classification = self.classifier.classify(item.text)
        source_count = self.storage.cluster_source_count(cluster_id)
        verified = source_count >= self.settings.min_sources_for_verified

        should_notify = (
            meets_min_strength(classification.strength, self.settings.min_notify_strength)
            and classification.sentiment != "neutral"
            and verified
            and not self.storage.is_notified(cluster_id)
        )

        if not should_notify:
            logger.info(
                "Skipping notification for %r (sentiment=%s strength=%s sources=%d verified=%s)",
                item.title,
                classification.sentiment,
                classification.strength,
                source_count,
                verified,
            )
            return

        price_move = recent_move(self.storage)
        message = format_message(item, classification, source_count, verified, price_move)
        if self.notifier.send(message):
            self.storage.mark_notified(cluster_id)

    def run_forever(self) -> None:  # pragma: no cover - long running loop
        logger.info("Starting GRAM/TON monitor loop")
        last_price_poll = 0.0
        while True:
            try:
                self.poll_news_once()
            except Exception:
                logger.exception("Error during news poll cycle")

            now = time.time()
            if now - last_price_poll >= self.settings.price_poll_interval_seconds:
                try:
                    self.poll_price()
                except Exception:
                    logger.exception("Error during price poll")
                last_price_poll = now

            time.sleep(self.settings.poll_interval_seconds)


def build_monitor(settings: Settings | None = None) -> GramTonMonitor:
    settings = settings or Settings.from_env()
    storage = Storage(settings.database_path)
    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    return GramTonMonitor(settings, storage, notifier)


def run() -> None:  # pragma: no cover - entry point
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    monitor = build_monitor()
    monitor.run_forever()
