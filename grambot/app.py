"""Application wiring and the main monitoring loop."""
from __future__ import annotations

import logging
import os
import signal
import threading
import time
from typing import List, Optional, Sequence, Tuple

from . import price as price_module
from .commands import MUTED_UNTIL_KEY, CommandHandler
from .config import Settings
from .notifier import TelegramNotifier, format_news_alert, format_price_alert, format_startup
from .price import PriceMove
from .processing.classifier import Classifier, RuleBasedClassifier, meets_min_strength
from .processing.clustering import find_matching_cluster, new_cluster_id
from .processing.filters import filter_fresh, filter_relevant
from .processing.llm_classifier import LLMClassifier
from .sources import NewsItem
from .sources.rss import FeedResult, RSSSource
from .storage import Storage

logger = logging.getLogger(__name__)

LAST_PRICE_ALERT_KEY = "last_price_alert_at"
LAST_PRUNE_KEY = "last_prune_at"


class GramTonMonitor:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        source: RSSSource,
        classifier: Classifier,
        notifier: TelegramNotifier,
        classifier_name: str = "rule-based",
    ):
        self.settings = settings
        self.storage = storage
        self.source = source
        self.classifier = classifier
        self.notifier = notifier
        self.classifier_name = classifier_name

        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.started_at = time.time()
        self.last_news_poll_at: Optional[float] = None
        self.last_price_poll_at: Optional[float] = None
        self.last_feed_results: List[FeedResult] = []
        self._poll_requested = False
        self._poll_lock = threading.Lock()
        self._price_lock = threading.Lock()
        self._trusted = [s.lower() for s in settings.trusted_sources if s.strip()]
        self.price_client = price_module.PriceClient(
            coin_id=settings.coingecko_coin_id,
            symbol=settings.price_symbol,
            display_currency=settings.display_currency,
        )
        self.fiat_rates = price_module.FiatRates()

    def _localize(self, move: Optional[PriceMove]) -> Optional[PriceMove]:
        if move is None:
            return None
        return price_module.with_local_currency(move, self.settings.display_currency, self.fiat_rates)

    # -- control ---------------------------------------------------------
    def request_poll(self) -> None:
        self._poll_requested = True
        self.wake_event.set()

    def stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()

    def muted_until(self) -> Optional[float]:
        until = self.storage.get_float(MUTED_UNTIL_KEY)
        if until and until > time.time():
            return until
        return None

    def can_notify(self) -> Tuple[bool, str]:
        if self.muted_until():
            return False, "muted"
        sent_last_hour = self.storage.count_signals_since(time.time() - 3600)
        if sent_last_hour >= self.settings.max_notifications_per_hour:
            return False, f"rate limit ({sent_last_hour}/h)"
        return True, ""

    def is_trusted_source(self, source: str) -> bool:
        name = source.lower()
        return any(t in name for t in self._trusted)

    # -- news ------------------------------------------------------------
    def poll_news_once(self) -> int:
        with self._poll_lock:
            results = self.source.fetch_all()
            self.last_feed_results = results
            self.last_news_poll_at = time.time()

            items: List[NewsItem] = [item for r in results for item in r.items]
            fresh = filter_fresh(items, self.settings.max_item_age_hours)
            relevant = filter_relevant(fresh, self.settings.keywords)
            # Oldest first so corroboration and clustering follow the timeline.
            relevant.sort(key=lambda i: i.published_at)

            notified = 0
            new_items = 0
            for item in relevant:
                if self.storage.has_seen(item.item_hash):
                    continue
                new_items += 1
                try:
                    if self._process_item(item):
                        notified += 1
                except Exception:  # pragma: no cover - one bad item must not stop the poll
                    logger.exception("Failed to process item %r", item.title)

            ok_feeds = sum(1 for r in results if r.ok)
            logger.info(
                "Poll done: feeds %d/%d ok, %d items, %d fresh, %d relevant, %d new, %d notified",
                ok_feeds, len(results), len(items), len(fresh), len(relevant), new_items, notified,
            )
            return notified

    def _process_item(self, item: NewsItem) -> bool:
        """Cluster, verify, classify and (maybe) notify. Returns True if sent."""
        window_start = item.published_at - self.settings.cluster_window_minutes * 60
        candidates = self.storage.recent_clusters(window_start)
        cluster_id = find_matching_cluster(
            item.title, [(c.cluster_id, c.representative_title) for c in candidates]
        )
        if cluster_id is None:
            cluster_id = new_cluster_id(item.title, item.item_hash[:12])
        self.storage.upsert_cluster(cluster_id, item.title, item.published_at)
        self.storage.mark_seen(item.item_hash, item.source, item.title, item.url, item.published_at, cluster_id)

        if self.storage.is_notified(cluster_id):
            logger.debug("Cluster %s already notified; skipping %r", cluster_id, item.title)
            return False

        sources = self.storage.cluster_sources(cluster_id)
        trusted = any(self.is_trusted_source(s) for s in sources)
        verified = trusted or len(sources) >= self.settings.min_sources_for_verified
        if not verified:
            logger.info("Unverified (%d source): %r [%s]", len(sources), item.title, item.source)
            return False

        classification = self.classifier.classify(item.text)
        if classification.sentiment == "neutral":
            logger.info("Neutral: %r [%s]", item.title, item.source)
            return False
        if not meets_min_strength(classification.strength, self.settings.min_notify_strength):
            logger.info("Below strength threshold (%s): %r", classification.strength, item.title)
            return False

        allowed, why = self.can_notify()
        if not allowed:
            logger.info("Suppressed (%s): %r", why, item.title)
            return False

        move = self.current_price_move()
        message = format_news_alert(
            item,
            classification,
            source_count=len(sources),
            sources=sources,
            verified=verified,
            price_move=move,
        )
        if not self.notifier.send(message):
            logger.warning("Telegram send failed for %r", item.title)
            return False

        self.storage.mark_notified(cluster_id)
        self.storage.record_signal(
            kind="news",
            title=item.title,
            url=item.url,
            sentiment=classification.sentiment,
            strength=classification.strength,
            source_count=len(sources),
            price_at_send=move.price_usd if move else None,
            cluster_id=cluster_id,
        )
        logger.info(
            "Notified: %r (sentiment=%s strength=%s sources=%d)",
            item.title, classification.sentiment, classification.strength, len(sources),
        )
        return True

    # -- price -------------------------------------------------------------
    def poll_price(self, alert: bool = True) -> Optional[PriceMove]:
        with self._price_lock:
            snapshot = self.price_client.fetch()
            if snapshot is None:
                logger.warning("All price providers failed (%s)", self.price_client.last_error)
                return None
            move = price_module.compute_move(self.storage, snapshot, self.settings.price_window_minutes)
            self.storage.add_price_point(
                snapshot.price_usd,
                snapshot.volume_24h_usd,
                snapshot.change_24h_pct,
                snapshot.fetched_at,
                provider=snapshot.provider,
            )
            self.last_price_poll_at = snapshot.fetched_at
        move = self._localize(move)
        if alert:
            self._maybe_price_alert(move)
        return move

    def _maybe_price_alert(self, move: PriceMove) -> bool:
        if not price_module.is_spike(move, self.settings.price_alert_threshold_pct):
            return False
        last = self.storage.get_float(LAST_PRICE_ALERT_KEY) or 0.0
        cooldown = self.settings.price_alert_cooldown_minutes * 60
        if time.time() - last < cooldown:
            logger.info("Price spike %.2f%% within cooldown; not alerting", move.window_change_pct or 0.0)
            return False
        allowed, why = self.can_notify()
        if not allowed:
            logger.info("Price alert suppressed (%s)", why)
            return False
        if not self.notifier.send(format_price_alert(move)):
            return False
        self.storage.set_value(LAST_PRICE_ALERT_KEY, str(time.time()))
        self.storage.record_signal(
            kind="price",
            title=f"TON {move.window_change_pct:+.2f}% за {move.window_minutes} мин",
            url=None,
            sentiment="positive" if (move.window_change_pct or 0) > 0 else "negative",
            strength="high" if abs(move.window_change_pct or 0) >= 2 * self.settings.price_alert_threshold_pct else "medium",
            source_count=0,
            price_at_send=move.price_usd,
        )
        logger.info("Price alert sent: %.2f%%", move.window_change_pct or 0.0)
        return True

    def current_price_move(self) -> Optional[PriceMove]:
        latest = self.storage.latest_price()
        max_age = 2 * self.settings.price_poll_interval_seconds
        if latest and time.time() - latest.fetched_at <= max_age:
            return self._localize(
                price_module.move_from_history(self.storage, self.settings.price_window_minutes)
            )
        return self.poll_price(alert=False)

    def update_signal_followups(self) -> int:
        """Fill in price 1h/24h after each sent signal (for /stats)."""
        now = time.time()
        updated = 0
        for column, delay, tolerance in (
            ("price_after_1h", 3600, 15 * 60),
            ("price_after_24h", 86400, 60 * 60),
        ):
            pending = self.storage.signals_missing_followup(column, now - delay, now - 7 * 86400)
            for sig in pending:
                point = self.storage.price_near(sig.sent_at + delay, tolerance)
                if point is not None:
                    self.storage.set_signal_followup(sig.id, column, point.price_usd)
                    updated += 1
        return updated

    def maybe_prune(self) -> None:
        last = self.storage.get_float(LAST_PRUNE_KEY) or 0.0
        if time.time() - last < 86400:
            return
        deleted = self.storage.prune(self.settings.retention_days)
        self.storage.set_value(LAST_PRUNE_KEY, str(time.time()))
        logger.info("Pruned %d old rows (retention %d days)", deleted, self.settings.retention_days)

    # -- lifecycle -----------------------------------------------------------
    def _install_signal_handlers(self) -> None:
        def _handler(signum, frame):  # pragma: no cover - process signal
            logger.info("Received signal %s, shutting down", signum)
            self.stop()

        try:
            signal.signal(signal.SIGINT, _handler)
            signal.signal(signal.SIGTERM, _handler)
        except ValueError:  # not in main thread
            pass

    def _safe(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:  # pragma: no cover - keep the loop alive
            logger.exception("%s failed", getattr(fn, "__name__", fn))
            return None

    def run_once(self) -> int:
        self._safe(self.poll_price)
        notified = self._safe(self.poll_news_once) or 0
        self._safe(self.update_signal_followups)
        return notified

    def run_forever(self) -> None:
        self._install_signal_handlers()
        logger.info(
            "Starting GRAM/TON monitor: %d feeds, classifier=%s, news every %ss, price every %ss",
            len(self.settings.rss_feeds), self.classifier_name,
            self.settings.poll_interval_seconds, self.settings.price_poll_interval_seconds,
        )
        if self.settings.send_startup_message and self.notifier.is_configured:
            self.notifier.send(
                format_startup(
                    len(self.settings.rss_feeds), self.settings.keywords, self.classifier_name,
                    self.settings.enable_commands,
                )
            )
        if self.settings.enable_commands and self.notifier.is_configured:
            CommandHandler(self, self.notifier).start()

        next_news = 0.0
        next_price = 0.0
        try:
            while not self.stop_event.is_set():
                now = time.time()
                if now >= next_price:
                    self._safe(self.poll_price)
                    self._safe(self.update_signal_followups)
                    next_price = time.time() + self.settings.price_poll_interval_seconds
                if now >= next_news or self._poll_requested:
                    self._poll_requested = False
                    self._safe(self.poll_news_once)
                    next_news = time.time() + self.settings.poll_interval_seconds
                self._safe(self.maybe_prune)

                timeout = max(1.0, min(next_news, next_price) - time.time())
                self.wake_event.wait(timeout)
                self.wake_event.clear()
        finally:
            logger.info("Monitor stopped")
            self.storage.close()
            self.notifier.close()


def build_classifier(settings: Settings) -> Classifier:
    if settings.openai_api_key:
        logger.info("Using LLM classifier (%s via %s)", settings.openai_model, settings.openai_base_url)
        return LLMClassifier(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            model=settings.openai_model,
            fallback=RuleBasedClassifier(),
        )
    return RuleBasedClassifier()


def classifier_label(settings: Settings) -> str:
    if settings.openai_api_key:
        return f"LLM ({settings.openai_model}) + правила"
    return "правила (EN/RU)"


def build_monitor(settings: Optional[Settings] = None, telegram: bool = True) -> GramTonMonitor:
    settings = settings or Settings.from_env()
    storage = Storage(settings.database_path)
    source = RSSSource(settings.rss_feeds, timeout=settings.feed_timeout_seconds)
    classifier = build_classifier(settings)
    notifier = TelegramNotifier(
        settings.telegram_bot_token if telegram else "",
        settings.telegram_chat_id if telegram else "",
    )
    if telegram and not notifier.is_configured:
        logger.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set: running in dry-run mode")
    return GramTonMonitor(settings, storage, source, classifier, notifier, classifier_name=classifier_label(settings))


def configure_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def run(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="GRAM/TON news monitor bot")
    parser.add_argument("--once", action="store_true", help="poll price and news once, then exit")
    parser.add_argument("--no-telegram", action="store_true", help="never send to Telegram (log messages instead)")
    args = parser.parse_args(argv)

    configure_logging()
    monitor = build_monitor(telegram=not args.no_telegram)
    if args.once:
        try:
            notified = monitor.run_once()
        finally:
            monitor.storage.close()
            monitor.notifier.close()
        logger.info("Single run finished, %d notification(s)", notified)
        return 0
    monitor.run_forever()
    return 0
