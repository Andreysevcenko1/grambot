"""Application wiring and the main monitoring loop."""
from __future__ import annotations

import faulthandler
import logging
import os
import signal
import sys
import threading
import time
from typing import List, Optional, Sequence, Tuple

from . import derivatives as derivatives_module
from . import market as market_module
from . import onchain as onchain_module
from . import price as price_module
from .commands import MUTED_UNTIL_KEY, CommandHandler
from .config import Settings
from .derivatives import FuturesClient, FuturesSnapshot, LiquidationStats
from .health import HealthEvent, HealthTracker, Watchdog, format_health_event, heartbeat_age, write_heartbeat
from .notifier import (
    TelegramNotifier,
    format_futures_alert,
    format_impulse_alert,
    format_network_alert,
    format_news_alert,
    format_price_alert,
    format_startup,
    format_volume_alert,
    format_whale_alert,
)
from .onchain import Labels, MasterchainState, ScanResult, TonCenterClient, Transfer
from .price import PriceMove
from .processing.classifier import Classifier, RuleBasedClassifier, meets_min_strength
from .processing.clustering import find_matching_cluster, new_cluster_id
from .processing.filters import filter_fresh, is_relevant
from .processing.keywords import matches_any
from .processing.llm_classifier import LLMClassifier
from .processing.whale_alert import is_transfer_post, is_whale_alert_item, parse_transfer
from .sources import NewsItem
from .sources.rss import FeedResult, RSSSource
from .sources.telegram_web import channel_from_post_url, channel_from_url
from .storage import SeenItem, Storage
from .supervisor import Supervisor, is_child_process, restart_info

logger = logging.getLogger(__name__)

LAST_PRICE_ALERT_KEY = "last_price_alert_at"
LAST_VOLUME_ALERT_KEY = "last_volume_alert_at"
LAST_IMPULSE_ALERT_KEY = "last_impulse_alert_at"
MARKET_ACTIVE_UNTIL_KEY = "market_active_until"
PRICE_WINDOW_FIRED_KEY = "price_window_fired_{minutes}"  # set while a window waits to re-arm
VOLUME_FIRED_KEY = "volume_alert_fired"
IMPULSE_FIRED_KEY = "impulse_alert_fired"
LAST_PRUNE_KEY = "last_prune_at"
ONCHAIN_LAST_UTIME_KEY = "onchain_last_utime"
LAST_WHALE_ALERT_KEY = "last_whale_alert_at"
NETWORK_STALLED_SINCE_KEY = "network_stalled_since"
FUTURES_FUNDING_FIRED_KEY = "futures_funding_fired"
FUTURES_OI_FIRED_KEY = "futures_oi_fired_{minutes}"
FUTURES_LIQ_FIRED_KEY = "futures_liq_fired"
LAST_FUTURES_ALERT_KEY = "last_futures_alert_{kind}"
WHALE_ALERT_CHANNEL = "whale_alert_io"
WHALE_ALERT_MAX_AGE_SECONDS = 1800  # older Whale Alert posts are recorded for /whales but not alerted
ONCHAIN_STUCK_FAILURES = 5
ONCHAIN_STUCK_SKIP_SECONDS = 60
COMMAND_HANDLER_RESTART_DELAY = 30.0


def _describe_hits(hits: Sequence[Tuple[int, float, float]]) -> str:
    return ", ".join(f"{change:+.2f}%/{minutes}m" for minutes, change, _ in hits)


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
        self._trusted_names, self._trusted_channels = self._parse_source_list(settings.trusted_sources)
        self._priority_names, self._priority_channels = self._parse_source_list(settings.priority_sources)
        # Fast lane: priority Telegram channels re-polled between full polls.
        self._priority_feed_urls: List[str] = [
            feed for feed in settings.rss_feeds
            if (channel_from_url(feed) or "").lower() in self._priority_channels
        ]
        self.priority_poll_enabled = bool(self._priority_feed_urls) and settings.priority_poll_interval_seconds > 0
        self.last_priority_poll_at: Optional[float] = None
        self.price_client = price_module.PriceClient(
            coin_id=settings.coingecko_coin_id,
            symbol=settings.price_symbol,
            display_currency=settings.display_currency,
        )
        self.fiat_rates = price_module.FiatRates()
        self.volume_client = market_module.VolumeClient(
            symbol=settings.price_symbol, ttl=float(settings.volume_poll_interval_seconds)
        )
        self._market_confirmed_at: List[float] = []  # send times of market-confirmed single-source news

        self.onchain_enabled = settings.enable_onchain
        self.ton_client = TonCenterClient(api_key=settings.toncenter_api_key)
        self.labels = Labels(url=settings.labels_url or None)
        self._onchain_lock = threading.Lock()
        self.last_onchain_poll_at: Optional[float] = None
        self.last_scan: Optional[ScanResult] = None
        self.last_masterchain: Optional[MasterchainState] = None
        self._onchain_failures = 0
        # Whale Alert's Telegram channel feeds the whale monitor without TON Center.
        self.whale_alert_enabled = any(
            (channel_from_url(feed) or "").lower() == WHALE_ALERT_CHANNEL for feed in settings.rss_feeds
        )

        self.futures_enabled = settings.enable_futures_alerts
        self.futures_client = FuturesClient(symbol=settings.price_symbol)
        self._futures_lock = threading.Lock()
        self.last_futures_poll_at: Optional[float] = None
        self.last_futures_snapshot: Optional[FuturesSnapshot] = None
        self.last_liquidations: Optional[LiquidationStats] = None
        self._futures_stored_at: float = 0.0

        self.health = HealthTracker(alert_after_seconds=settings.health_alert_minutes * 60)
        self.heartbeat_at = time.monotonic()
        self.exit_code = 0
        self.exit_reason: Optional[str] = None
        self.command_handler: Optional[CommandHandler] = None
        self.restart_count, self.last_exit_code = restart_info()

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

    def request_restart(self, code: int, reason: str) -> None:
        """Stop with a non-zero code so the supervisor starts a fresh process."""
        self.exit_code = code
        self.exit_reason = reason
        self.stop()

    def beat(self) -> None:
        self.heartbeat_at = time.monotonic()
        write_heartbeat(self.settings.heartbeat_file)

    def _report_health(self, component: str, ok: bool, detail: Optional[str] = None) -> None:
        event = self.health.report(component, ok, detail)
        if event is not None:
            self._notify_health(event)

    def _notify_health(self, event: HealthEvent) -> None:
        text = format_health_event(event)
        logger.warning("Health: %s", text.replace("\n", " "))
        if self.notifier.is_configured and not self.muted_until():
            self.notifier.send(text)

    def muted_until(self) -> Optional[float]:
        until = self.storage.get_float(MUTED_UNTIL_KEY)
        if until and until > time.time():
            return until
        return None

    def can_notify(self, priority: bool = False) -> Tuple[bool, str]:
        """Priority posts skip the hourly cap (a founder's post is the one
        message worth receiving during a busy hour) but respect /mute."""
        if self.muted_until():
            return False, "muted"
        if priority:
            return True, ""
        sent_last_hour = self.storage.count_signals_since(time.time() - 3600)
        if sent_last_hour >= self.settings.max_notifications_per_hour:
            return False, f"rate limit ({sent_last_hour}/h)"
        return True, ""

    @staticmethod
    def _parse_source_list(entries: Sequence[str]) -> Tuple[List[str], set]:
        """Split a source list into lowercase name fragments and Telegram channel usernames."""
        names: List[str] = []
        channels: set = set()
        for entry in entries:
            entry = entry.strip()
            if not entry:
                continue
            channel = channel_from_url(entry)
            if channel:
                channels.add(channel.lower())
            else:
                names.append(entry.lower())
        return names, channels

    @staticmethod
    def _source_matches(source: str, url: str, names: Sequence[str], channels: set) -> bool:
        name = source.lower()
        if any(t in name for t in names):
            return True
        channel = channel_from_post_url(url) if url else None
        return bool(channel) and channel.lower() in channels

    def is_trusted_source(self, source: str, url: str = "") -> bool:
        """``source`` is matched by name; Telegram posts also by channel username."""
        return self._source_matches(source, url, self._trusted_names, self._trusted_channels)

    def is_priority_source(self, source: str, url: str = "") -> bool:
        """Founder/official channels whose posts are shown even without a sentiment reading."""
        return self._source_matches(source, url, self._priority_names, self._priority_channels)

    def _is_priority_item(self, item: NewsItem) -> bool:
        """A priority-source post that mentions the ecosystem (``priority_keywords``)."""
        return self.is_priority_source(item.source, item.url or "") and bool(
            matches_any(item.text, self.settings.priority_keywords)
        )

    # -- news ------------------------------------------------------------
    def poll_news_once(self) -> int:
        """Full poll of every feed."""
        with self._poll_lock:
            results = self.source.fetch_all()
            self.last_feed_results = results
            self.last_news_poll_at = time.time()
            notified, ok_feeds, whale_transfers = self._process_feed_results(results, "Poll")
        if results:
            first_error = next((r.error for r in results if not r.ok and r.error), None)
            self._report_health("feeds", ok_feeds > 0, first_error)
        self._alert_whale_transfers(whale_transfers)
        return notified

    def poll_priority_once(self) -> int:
        """Fast lane: only the priority Telegram channels, between full polls."""
        if not self._priority_feed_urls:
            return 0
        with self._poll_lock:
            results = self.source.fetch_all(self._priority_feed_urls)
            self.last_feed_results = list(getattr(self.source, "last_results", None) or self.last_feed_results)
            self.last_priority_poll_at = time.time()
            notified, _ok, whale_transfers = self._process_feed_results(results, "Priority poll")
        self._alert_whale_transfers(whale_transfers)
        return notified

    def _process_feed_results(self, results: Sequence[FeedResult], label: str) -> Tuple[int, int, List[Transfer]]:
        """Filter, cluster, classify and notify; caller holds ``_poll_lock``."""
        items: List[NewsItem] = [item for r in results for item in r.items]
        fresh = filter_fresh(items, self.settings.max_item_age_hours)
        # Whale Alert transfer lines are data, not headlines: route them to
        # the whale monitor and keep them out of clustering/verification.
        transfer_posts = [item for item in fresh if is_whale_alert_item(item) and is_transfer_post(item)]
        if transfer_posts:
            skipped = {id(item) for item in transfer_posts}
            fresh = [item for item in fresh if id(item) not in skipped]
        # Priority channels are also relevant when they touch the ecosystem
        # without naming it ("Stars can now be cashed out to your wallet").
        relevant = [item for item in fresh if is_relevant(item, self.settings.keywords) or self._is_priority_item(item)]
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

        whale_transfers = self._ingest_whale_alert_posts(transfer_posts)

        ok_feeds = sum(1 for r in results if r.ok)
        log = logger.info if label == "Poll" or new_items else logger.debug
        log(
            "%s done: feeds %d/%d ok, %d items, %d fresh, %d relevant, %d new, %d notified, %d whale transfer(s)",
            label, ok_feeds, len(results), len(items), len(fresh), len(relevant), new_items, notified, len(whale_transfers),
        )
        return notified, ok_feeds, whale_transfers

    def _alert_whale_transfers(self, whale_transfers: Sequence[Transfer]) -> None:
        for transfer in whale_transfers:
            if transfer.amount_ton < self.settings.whale_min_ton or transfer.kind not in onchain_module.ALERT_KINDS:
                continue
            if time.time() - transfer.utime > WHALE_ALERT_MAX_AGE_SECONDS:
                # First poll after a (re)start sees the channel's whole recent page.
                logger.info("Whale Alert transfer %.0f TON is %.0f min old; recorded without alert", transfer.amount_ton, (time.time() - transfer.utime) / 60)
                continue
            self._safe(self._maybe_whale_alert, transfer)

    def _ingest_whale_alert_posts(self, posts: Sequence[NewsItem]) -> List[Transfer]:
        """Record new TON/GRAM transfers reported by Whale Alert; returns them oldest first."""
        transfers: List[Transfer] = []
        for item in sorted(posts, key=lambda i: i.published_at):
            try:
                transfer = parse_transfer(item)
            except Exception:  # pragma: no cover - a malformed post must not stop the poll
                logger.exception("Failed to parse Whale Alert post %r", item.title)
                continue
            if transfer is None:
                continue  # another coin
            inserted = self.storage.record_transfer(
                transfer.hash,
                transfer.utime,
                transfer.source,
                transfer.destination,
                transfer.amount_ton,
                transfer.source_label.display() if transfer.source_label else None,
                transfer.destination_label.display() if transfer.destination_label else None,
                transfer.kind,
                link=transfer.link,
            )
            if inserted:
                transfers.append(transfer)
                logger.info("Whale Alert: %.0f TON %s (%s → %s)", transfer.amount_ton, transfer.kind, transfer.source, transfer.destination)
        return transfers

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
        priority = self.is_priority_source(item.source, item.url or "")
        trusted = priority or self.is_trusted_source(item.source, item.url or "") or any(
            self.is_trusted_source(s) for s in sources
        )
        verified = trusted or len(sources) >= self.settings.min_sources_for_verified
        market_confirmed = False
        if not verified and self._market_can_confirm(item):
            # The market is already moving: a lone, fresh headline is worth
            # showing now (labelled as such) instead of waiting for a second source.
            verified = market_confirmed = True
        if not verified:
            logger.info("Unverified (%d source): %r [%s]", len(sources), item.title, item.source)
            return False

        classification = self.classifier.classify(item.text)
        if classification.sentiment == "neutral":
            # A founder/official post about the ecosystem is shown even when the
            # lexicon finds nothing to grade ("Stars can now be converted to Toncoin").
            priority_hits = matches_any(item.text, self.settings.priority_keywords) if priority else []
            if not priority_hits:
                logger.info("Neutral: %r [%s]", item.title, item.source)
                return False
            logger.info("Priority post without sentiment reading (%s): %r [%s]", ", ".join(priority_hits), item.title, item.source)
        elif not priority and not meets_min_strength(classification.strength, self.settings.min_notify_strength):
            logger.info("Below strength threshold (%s): %r", classification.strength, item.title)
            return False

        allowed, why = self.can_notify(priority=priority)
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
            market_confirmed=market_confirmed,
            priority=priority,
        )
        if not self.notifier.send(message):
            logger.warning("Telegram send failed for %r", item.title)
            return False

        self.storage.mark_notified(cluster_id)
        if market_confirmed:
            self._market_confirmed_at.append(time.time())
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
            "Notified: %r (sentiment=%s strength=%s sources=%d%s%s)",
            item.title, classification.sentiment, classification.strength, len(sources),
            " market-confirmed" if market_confirmed else "",
            " priority" if priority else "",
        )
        return True

    def market_active_until(self) -> Optional[float]:
        until = self.storage.get_float(MARKET_ACTIVE_UNTIL_KEY)
        if until and until > time.time():
            return until
        return None

    def _mark_market_active(self) -> None:
        until = time.time() + self.settings.market_active_minutes * 60
        self.storage.set_value(MARKET_ACTIVE_UNTIL_KEY, str(until))

    def _market_can_confirm(self, item: NewsItem) -> bool:
        if not self.settings.market_confirms_news or not self.market_active_until():
            return False
        cutoff = time.time() - self.settings.market_active_minutes * 60
        if item.published_at < cutoff:
            return False  # an old headline cannot be "confirmed" by today's move
        self._market_confirmed_at = [t for t in self._market_confirmed_at if t >= cutoff]
        return len(self._market_confirmed_at) < self.settings.market_confirmed_news_limit

    def recent_causes(self, hours: float = 3.0, limit: int = 2) -> List[SeenItem]:
        """Latest relevant headlines (one per cluster) that may explain a move."""
        items = self.storage.recent_items(time.time() - hours * 3600, limit=20)
        causes: List[SeenItem] = []
        seen_clusters = set()
        for item in items:
            key = item.cluster_id or item.title
            if key in seen_clusters:
                continue
            seen_clusters.add(key)
            causes.append(item)
            if len(causes) >= limit:
                break
        return causes

    # -- price -------------------------------------------------------------
    def poll_price(self, alert: bool = True) -> Optional[PriceMove]:
        with self._price_lock:
            snapshot = self.price_client.fetch()
            if snapshot is None:
                logger.warning("All price providers failed (%s)", self.price_client.last_error)
                self._report_health("price", False, self.price_client.last_error)
                return None
            self._report_health("price", True)
            move = price_module.compute_move(
                self.storage, snapshot, self.settings.price_window_minutes, self.settings.price_window_lengths
            )
            self.storage.add_price_point(
                snapshot.price_usd,
                snapshot.volume_24h_usd,
                snapshot.change_24h_pct,
                snapshot.fetched_at,
                provider=snapshot.provider,
            )
            self.last_price_poll_at = snapshot.fetched_at
            self._attach_volume(move)
        move = self._localize(move)
        self._maybe_market_alert(move, send=alert)
        return move

    def _attach_volume(self, move: PriceMove, force: bool = False) -> PriceMove:
        if not self.settings.enable_volume_alerts:
            return move
        try:
            stats = self.volume_client.fetch(force=force)
        except Exception as exc:  # pragma: no cover - defensive: volume is optional
            logger.warning("Volume fetch failed unexpectedly: %s", exc)
            stats = None
        return market_module.attach_volume(move, stats)

    def _window_fired(self, minutes: int) -> bool:
        return self.storage.get_value(PRICE_WINDOW_FIRED_KEY.format(minutes=minutes)) is not None

    def _set_window_fired(self, minutes: int, fired: bool) -> None:
        self.storage.set_value(PRICE_WINDOW_FIRED_KEY.format(minutes=minutes), "1" if fired else None)

    def _rearm_windows(self, move: PriceMove) -> None:
        """A window that already alerted re-arms once its change halves."""
        for minutes, threshold in self.settings.all_price_windows:
            change = move.window_changes.get(minutes)
            if change is not None and abs(change) < threshold / 2 and self._window_fired(minutes):
                self._set_window_fired(minutes, False)
                logger.info("Price window %d min re-armed (%.2f%%)", minutes, change)
        ratio = move.volume_1h_ratio
        if ratio is not None and ratio < self.settings.volume_spike_ratio / 2:
            if self.storage.get_value(VOLUME_FIRED_KEY) is not None:
                self.storage.set_value(VOLUME_FIRED_KEY, None)
                logger.info("Volume alert re-armed (ratio %.2f)", ratio)
        fast = move.window_changes.get(self.settings.price_window_minutes)
        if fast is not None and abs(fast) < self.settings.impulse_fast_pct / 2:
            if self.storage.get_value(IMPULSE_FIRED_KEY) is not None:
                self.storage.set_value(IMPULSE_FIRED_KEY, None)
                logger.info("Impulse alert re-armed (%.2f%%)", fast)

    def _maybe_market_alert(self, move: PriceMove, send: bool = True) -> bool:
        """Multi-window price alerts, volume-burst alerts and the early-warning
        impulse, with hysteresis.

        Every window that crosses its threshold marks the market as active (so
        single-source news can be confirmed) even when the alert itself is held
        back by a cooldown or ``send`` is False; a window alerts once and
        re-arms after its change drops below half the threshold. The impulse is
        weaker evidence: it neither activates the market nor shares the price
        alert cooldown, so the regular alert still follows if the move goes on.
        """
        self._rearm_windows(move)
        now = time.time()
        s = self.settings
        hits = market_module.triggered_windows(move, s.all_price_windows)
        volume_spike = s.enable_volume_alerts and market_module.is_volume_spike(
            move, s.volume_spike_ratio, s.volume_spike_min_move_pct
        )
        impulse = (
            market_module.impulse(move, s.price_window_minutes, s.impulse_fast_pct, s.impulse_slow_pct)
            if s.enable_impulse_alerts
            else None
        )
        if not hits and not volume_spike and impulse is None:
            return False
        if hits or volume_spike:
            self._mark_market_active()
        if not send:
            return False

        armed_hits = [h for h in hits if not self._window_fired(h[0])]
        volume_armed = volume_spike and self.storage.get_value(VOLUME_FIRED_KEY) is None
        sent = False
        last_price_alert = self.storage.get_float(LAST_PRICE_ALERT_KEY) or 0.0
        if armed_hits:
            if now - last_price_alert < s.price_alert_cooldown_minutes * 60:
                logger.info("Price move %s within cooldown; not alerting", _describe_hits(armed_hits))
            else:
                sent = self._send_price_alert(move, hits, armed_hits)
        if volume_spike and not sent and volume_armed:
            last = self.storage.get_float(LAST_VOLUME_ALERT_KEY) or 0.0
            if now - last < s.volume_alert_cooldown_minutes * 60:
                logger.info("Volume spike x%.1f within cooldown; not alerting", move.volume_1h_ratio or 0.0)
            else:
                sent = self._send_volume_alert(move)
        if impulse is not None and not sent and self.storage.get_value(IMPULSE_FIRED_KEY) is None:
            cooldown = s.impulse_cooldown_minutes * 60
            last_impulse = self.storage.get_float(LAST_IMPULSE_ALERT_KEY) or 0.0
            # A regular price alert in the last hour already covered this move.
            if now - last_impulse < cooldown or now - last_price_alert < cooldown:
                logger.info("Impulse %+.2f%% within cooldown; not alerting", impulse[0])
            else:
                sent = self._send_impulse_alert(move, impulse)
        return sent

    def _send_price_alert(
        self,
        move: PriceMove,
        hits: Sequence[Tuple[int, float, float]],
        armed_hits: Sequence[Tuple[int, float, float]],
    ) -> bool:
        allowed, why = self.can_notify()
        if not allowed:
            logger.info("Price alert suppressed (%s)", why)
            return False
        minutes, change, threshold = market_module.strongest_window(armed_hits)
        causes = self.recent_causes()
        if not self.notifier.send(format_price_alert(move, minutes, causes)):
            return False
        now = time.time()
        self.storage.set_value(LAST_PRICE_ALERT_KEY, str(now))
        for hit_minutes, _, _ in hits:  # the message covers every window that is currently over its threshold
            self._set_window_fired(hit_minutes, True)
        if move.volume_1h_ratio is not None and move.volume_1h_ratio >= self.settings.volume_spike_ratio:
            self.storage.set_value(VOLUME_FIRED_KEY, "1")  # volume is already shown in this alert
            self.storage.set_value(LAST_VOLUME_ALERT_KEY, str(now))
        self.storage.record_signal(
            kind="price",
            title=f"TON {change:+.2f}% за {minutes} мин",
            url=None,
            sentiment="positive" if change > 0 else "negative",
            strength="high" if abs(change) >= 2 * threshold else "medium",
            source_count=len(causes),
            price_at_send=move.price_usd,
        )
        logger.info("Price alert sent: %s (causes: %d)", _describe_hits(hits), len(causes))
        return True

    def _send_volume_alert(self, move: PriceMove) -> bool:
        allowed, why = self.can_notify()
        if not allowed:
            logger.info("Volume alert suppressed (%s)", why)
            return False
        causes = self.recent_causes()
        if not self.notifier.send(format_volume_alert(move, causes)):
            return False
        now = time.time()
        self.storage.set_value(LAST_VOLUME_ALERT_KEY, str(now))
        self.storage.set_value(VOLUME_FIRED_KEY, "1")
        change_1h = move.window_changes.get(60) or 0.0
        self.storage.record_signal(
            kind="volume",
            title=f"Объём TON ×{move.volume_1h_ratio or 0:.1f} за час ({change_1h:+.2f}%)",
            url=None,
            sentiment="positive" if change_1h > 0 else ("negative" if change_1h < 0 else "unknown"),
            strength="high" if (move.volume_1h_ratio or 0) >= 2 * self.settings.volume_spike_ratio else "medium",
            source_count=len(causes),
            price_at_send=move.price_usd,
        )
        logger.info("Volume alert sent: x%.1f, 1h %+.2f%% (causes: %d)", move.volume_1h_ratio or 0.0, change_1h, len(causes))
        return True

    def _send_impulse_alert(self, move: PriceMove, impulse: Tuple[float, float]) -> bool:
        allowed, why = self.can_notify()
        if not allowed:
            logger.info("Impulse alert suppressed (%s)", why)
            return False
        fast, slow = impulse
        minutes = self.settings.price_window_minutes
        causes = self.recent_causes()
        if not self.notifier.send(format_impulse_alert(move, minutes, causes)):
            return False
        now = time.time()
        self.storage.set_value(LAST_IMPULSE_ALERT_KEY, str(now))
        self.storage.set_value(IMPULSE_FIRED_KEY, "1")
        self.storage.record_signal(
            kind="impulse",
            title=f"Импульс TON {fast:+.2f}% за {minutes} мин ({slow:+.2f}% за час)",
            url=None,
            sentiment="positive" if fast > 0 else "negative",
            strength="high" if abs(fast) >= 2 * self.settings.impulse_fast_pct else "low",
            source_count=len(causes),
            price_at_send=move.price_usd,
        )
        logger.info("Impulse alert sent: %+.2f%%/%dm, 1h %+.2f%% (causes: %d)", fast, minutes, slow, len(causes))
        return True

    # Backwards-compatible name used by older callers/tests.
    _maybe_price_alert = _maybe_market_alert

    def current_price_move(self) -> Optional[PriceMove]:
        latest = self.storage.latest_price()
        max_age = 2 * self.settings.price_poll_interval_seconds
        if latest and time.time() - latest.fetched_at <= max_age:
            move = price_module.move_from_history(
                self.storage, self.settings.price_window_minutes, self.settings.price_window_lengths
            )
            if move is not None:
                market_module.attach_volume(move, self.volume_client.last_stats)
            return self._localize(move)
        return self.poll_price(alert=False)

    # -- on-chain ------------------------------------------------------------
    def poll_onchain(self, alert: bool = True) -> Optional[ScanResult]:
        """Check network health and scan for large transfers since the last run."""
        if not self.onchain_enabled:
            return None
        with self._onchain_lock:
            self.last_onchain_poll_at = time.time()
            self.labels.refresh_if_stale()
            if time.time() < self.ton_client.backoff_until:
                logger.info("toncenter backoff active for %.0fs; skipping on-chain poll", self.ton_client.backoff_until - time.time())
                return None
            self.ton_client.last_error = None
            self._check_network(alert)
            since = self.storage.get_float(ONCHAIN_LAST_UTIME_KEY)
            if since is None:
                # Start slightly in the past so the first poll produces a real sample.
                since = time.time() - 60
            result = onchain_module.scan_transfers(
                self.ton_client,
                self.labels,
                since,
                min_ton=self.settings.whale_min_ton / 10.0,
                max_pages=self.settings.onchain_max_pages,
            )
            self.last_scan = result
            if result.gap_seconds:
                logger.warning("On-chain scan fell behind; skipped %.0f min of history", result.gap_seconds / 60)
            if result.last_utime > since:
                self.storage.set_value(ONCHAIN_LAST_UTIME_KEY, str(result.last_utime))
            self._track_onchain_progress(result, since)
            new_transfers: List[Transfer] = []
            for transfer in result.transfers:
                inserted = self.storage.record_transfer(
                    transfer.hash,
                    transfer.utime,
                    transfer.source,
                    transfer.destination,
                    transfer.amount_ton,
                    transfer.source_label.display() if transfer.source_label else None,
                    transfer.destination_label.display() if transfer.destination_label else None,
                    transfer.kind,
                )
                if inserted:
                    new_transfers.append(transfer)
            logger.info(
                "On-chain scan: %d page(s), %d message(s), %d transfer(s) ≥ %.0f TON%s",
                result.pages, result.messages, len(new_transfers), self.settings.whale_min_ton / 10.0,
                "" if result.complete else " (incomplete)",
            )
        if alert:
            for transfer in new_transfers:
                if transfer.amount_ton >= self.settings.whale_min_ton and transfer.kind in onchain_module.ALERT_KINDS:
                    self._maybe_whale_alert(transfer)
        return result

    def _track_onchain_progress(self, result: ScanResult, since: float) -> None:
        """Skip a stuck window after repeated non-rate-limit failures; report health."""
        if result.error is None:
            self._onchain_failures = 0
            self._report_health("onchain", True)
            return
        if result.rate_limited:
            return  # expected on the free tier; backoff handles it
        self._report_health("onchain", False, result.error)
        if result.last_utime > since:
            self._onchain_failures = 0
            return
        self._onchain_failures += 1
        if self._onchain_failures >= ONCHAIN_STUCK_FAILURES:
            skip_to = since + ONCHAIN_STUCK_SKIP_SECONDS
            self.storage.set_value(ONCHAIN_LAST_UTIME_KEY, str(skip_to))
            self._onchain_failures = 0
            logger.warning("On-chain scan stuck at %.0f after %d failures; skipping %ds", since, ONCHAIN_STUCK_FAILURES, ONCHAIN_STUCK_SKIP_SECONDS)

    def _maybe_whale_alert(self, transfer: Transfer) -> bool:
        last = self.storage.get_float(LAST_WHALE_ALERT_KEY) or 0.0
        cooldown = self.settings.onchain_alert_cooldown_minutes * 60
        # Very large transfers (2× the threshold) bypass the cooldown.
        if time.time() - last < cooldown and transfer.amount_ton < 2 * self.settings.whale_min_ton:
            logger.info("Whale transfer %.0f TON within cooldown; not alerting", transfer.amount_ton)
            return False
        allowed, why = self.can_notify()
        if not allowed:
            logger.info("Whale alert suppressed (%s)", why)
            return False
        sentiment, strength = onchain_module.transfer_sentiment(transfer, self.settings.whale_min_ton)
        move = self._safe(self.current_price_move)
        if not self.notifier.send(format_whale_alert(transfer, sentiment, strength, move)):
            return False
        self.storage.set_value(LAST_WHALE_ALERT_KEY, str(time.time()))
        self.storage.mark_transfer_notified(transfer.hash)
        src = transfer.source_label.name if transfer.source_label else "?"
        dst = transfer.destination_label.name if transfer.destination_label else "?"
        self.storage.record_signal(
            kind="onchain",
            title=f"Перевод {transfer.amount_ton:,.0f} TON: {src} → {dst} ({transfer.kind})".replace(",", " "),
            url=transfer.url,
            sentiment=sentiment,
            strength=strength,
            source_count=1,
            price_at_send=move.price_usd if move else None,
        )
        logger.info("Whale alert sent: %.0f TON %s", transfer.amount_ton, transfer.kind)
        return True

    def _check_network(self, alert: bool) -> Optional[MasterchainState]:
        try:
            state = self.ton_client.masterchain_state()
        except onchain_module.RateLimited as exc:
            self.ton_client.backoff_until = time.time() + min(exc.retry_after, 3600.0)
            self.ton_client.last_error = str(exc)
            return None
        except Exception as exc:  # network / API errors are not a chain stall
            self.ton_client.last_error = str(exc)
            logger.warning("toncenter masterchainInfo failed: %s", exc)
            return None
        self.last_masterchain = state
        stalled_since = self.storage.get_float(NETWORK_STALLED_SINCE_KEY)
        threshold = self.settings.network_stall_minutes * 60
        if state.age_seconds >= threshold:
            if stalled_since is None:
                self.storage.set_value(NETWORK_STALLED_SINCE_KEY, str(state.gen_utime))
                logger.warning("TON masterchain block #%d is %.0fs old", state.seqno, state.age_seconds)
                if alert and self.can_notify()[0] and self.notifier.send(format_network_alert(state.age_seconds, state.seqno)):
                    self.storage.record_signal(
                        kind="onchain",
                        title=f"Остановка сети TON: блок #{state.seqno} устарел на {state.age_seconds / 60:.0f} мин",
                        url="https://tonstat.us",
                        sentiment="negative",
                        strength="high",
                        source_count=1,
                        price_at_send=None,
                    )
        elif stalled_since is not None:
            self.storage.set_value(NETWORK_STALLED_SINCE_KEY, None)
            pause = max(0.0, state.gen_utime - stalled_since)
            logger.info("TON masterchain resumed after %.0fs", pause)
            if alert and self.notifier.is_configured:
                self.notifier.send(format_network_alert(pause, state.seqno, recovered=True))
        return state

    def onchain_status(self) -> dict:
        """Summary for /status: scan lag, last block age, backoff state."""
        info = {
            "enabled": self.onchain_enabled,
            "labels": len(self.labels),
            "last_poll_at": self.last_onchain_poll_at,
            "lag_seconds": None,
            "block_age_seconds": None,
            "seqno": None,
            "error": self.ton_client.last_error,
            "backoff_seconds": max(0.0, self.ton_client.backoff_until - time.time()),
        }
        last_utime = self.storage.get_float(ONCHAIN_LAST_UTIME_KEY)
        if last_utime:
            info["lag_seconds"] = max(0.0, time.time() - last_utime)
        if self.last_masterchain:
            info["block_age_seconds"] = max(0.0, time.time() - self.last_masterchain.gen_utime)
            info["seqno"] = self.last_masterchain.seqno
        return info

    # -- derivatives -----------------------------------------------------------
    def poll_futures(self, alert: bool = True) -> Optional[FuturesSnapshot]:
        """Funding / open interest snapshot (+ OKX liquidations), history and alerts."""
        if not self.futures_enabled:
            return None
        with self._futures_lock:
            self.last_futures_poll_at = time.time()
            snapshot = self.futures_client.fetch()
            if snapshot is None:
                if self.futures_client.last_error:
                    logger.warning("All futures providers failed (%s)", self.futures_client.last_error)
                    self._report_health("futures", False, self.futures_client.last_error)
                return None
            self._report_health("futures", True)
            if snapshot.fetched_at != self._futures_stored_at:  # fetch() may return the cached snapshot
                self.storage.add_futures_point(
                    snapshot.provider,
                    snapshot.open_interest,
                    snapshot.open_interest_usd,
                    snapshot.funding_rate,
                    snapshot.mark_price,
                    fetched_at=snapshot.fetched_at,
                )
                self._futures_stored_at = snapshot.fetched_at
            snapshot.oi_changes = derivatives_module.oi_changes(
                self.storage, snapshot, [minutes for minutes, _ in self.settings.oi_alert_windows]
            )
            self.last_futures_snapshot = snapshot
            liquidations = None
            if self.settings.liquidation_alert_usd > 0:
                liquidations = self.futures_client.fetch_liquidations() or self.futures_client.last_liquidations
                self.last_liquidations = liquidations
            logger.info(
                "Futures (%s): funding %s/day, OI %s, changes %s, liquidations/h %s",
                snapshot.provider,
                f"{snapshot.funding_daily_pct:+.3f}%" if snapshot.funding_daily_pct is not None else "n/a",
                f"{snapshot.open_interest:,.0f}" if snapshot.open_interest is not None else "n/a",
                {m: round(c, 2) for m, c in snapshot.oi_changes.items()} or "-",
                f"${liquidations.total_usd:,.0f}" if liquidations else "n/a",
            )
        if alert:
            self._maybe_futures_alert(snapshot, liquidations)
        return snapshot

    def _futures_fired(self, key: str) -> bool:
        return self.storage.get_value(key) is not None

    def _rearm_futures(self, snapshot: FuturesSnapshot, liquidations: Optional[LiquidationStats]) -> None:
        """Each trigger re-arms once its reading falls below half the threshold."""
        s = self.settings
        daily = snapshot.funding_daily_pct
        if daily is not None and abs(daily) < s.funding_alert_daily_pct / 2 and self._futures_fired(FUTURES_FUNDING_FIRED_KEY):
            self.storage.set_value(FUTURES_FUNDING_FIRED_KEY, None)
            logger.info("Funding alert re-armed (%.3f%%/day)", daily)
        for minutes, threshold in s.oi_alert_windows:
            change = snapshot.oi_changes.get(minutes)
            key = FUTURES_OI_FIRED_KEY.format(minutes=minutes)
            if change is not None and abs(change) < threshold / 2 and self._futures_fired(key):
                self.storage.set_value(key, None)
                logger.info("OI window %d min re-armed (%.2f%%)", minutes, change)
        if liquidations is not None and liquidations.total_usd < s.liquidation_alert_usd / 2 and self._futures_fired(FUTURES_LIQ_FIRED_KEY):
            self.storage.set_value(FUTURES_LIQ_FIRED_KEY, None)
            logger.info("Liquidation alert re-armed ($%.0f/h)", liquidations.total_usd)

    def _futures_ready(self, kind: str, fired_key: str) -> bool:
        if self._futures_fired(fired_key):
            return False
        last = self.storage.get_float(LAST_FUTURES_ALERT_KEY.format(kind=kind)) or 0.0
        return time.time() - last >= self.settings.futures_alert_cooldown_minutes * 60

    def _maybe_futures_alert(self, snapshot: FuturesSnapshot, liquidations: Optional[LiquidationStats]) -> bool:
        """One informational message per poll covering every derivatives trigger
        that is currently over its threshold; each trigger has hysteresis and a
        per-type cooldown so a lasting condition is reported once."""
        s = self.settings
        self._rearm_futures(snapshot, liquidations)
        funding_hit = derivatives_module.funding_is_extreme(snapshot, s.funding_alert_daily_pct)
        oi_hits = derivatives_module.triggered_oi_windows(snapshot, s.oi_alert_windows)
        liq_hit = (
            liquidations is not None and s.liquidation_alert_usd > 0 and liquidations.total_usd >= s.liquidation_alert_usd
        )
        if not (funding_hit or oi_hits or liq_hit):
            return False
        if oi_hits or liq_hit:
            self._mark_market_active()  # leverage is moving: single-source news may be confirmed

        funding_armed = funding_hit and self._futures_ready("funding", FUTURES_FUNDING_FIRED_KEY)
        oi_armed = [hit for hit in oi_hits if self._futures_ready("oi", FUTURES_OI_FIRED_KEY.format(minutes=hit[0]))]
        liq_armed = liq_hit and self._futures_ready("liquidations", FUTURES_LIQ_FIRED_KEY)
        if not (funding_armed or oi_armed or liq_armed):
            logger.info("Futures triggers (funding=%s oi=%s liq=%s) already reported or in cooldown", funding_hit, bool(oi_hits), liq_hit)
            return False
        allowed, why = self.can_notify()
        if not allowed:
            logger.info("Futures alert suppressed (%s)", why)
            return False

        oi_hit = derivatives_module.strongest_oi_window(oi_armed or oi_hits)
        move = self._safe(self.current_price_move)
        causes = self.recent_causes() if liq_armed else []
        asset = derivatives_module.base_asset(s.price_symbol)
        message = format_futures_alert(
            snapshot,
            liquidations,
            asset=asset,
            funding_hit=funding_hit,
            oi_hit=oi_hit,
            liquidation_hit=liq_hit and liq_armed,
            move=move,
            causes=causes,
        )
        if not self.notifier.send(message):
            return False
        now = time.time()
        if funding_hit:
            self.storage.set_value(FUTURES_FUNDING_FIRED_KEY, "1")
            self.storage.set_value(LAST_FUTURES_ALERT_KEY.format(kind="funding"), str(now))
        for minutes, _, _ in oi_hits:  # the message shows every window that is over its threshold
            self.storage.set_value(FUTURES_OI_FIRED_KEY.format(minutes=minutes), "1")
        if oi_hits:
            self.storage.set_value(LAST_FUTURES_ALERT_KEY.format(kind="oi"), str(now))
        if liq_hit:
            self.storage.set_value(FUTURES_LIQ_FIRED_KEY, "1")
            self.storage.set_value(LAST_FUTURES_ALERT_KEY.format(kind="liquidations"), str(now))

        if liq_armed and liquidations is not None:
            title = f"Ликвидации ${liquidations.total_usd:,.0f} за час (лонги {int((liquidations.long_share or 0) * 100)}%)".replace(",", " ")
            strength = "high" if liquidations.total_usd >= 2 * s.liquidation_alert_usd else "medium"
        elif oi_hit is not None and oi_armed:
            title = f"Открытый интерес {oi_hit[1]:+.1f}% за {oi_hit[0]} мин"
            strength = "high" if abs(oi_hit[1]) >= 2 * oi_hit[2] else "medium"
        else:
            title = f"Ставка финансирования {snapshot.funding_daily_pct or 0:+.2f}%/день"
            strength = "high" if abs(snapshot.funding_daily_pct or 0) >= 2 * s.funding_alert_daily_pct else "medium"
        self.storage.record_signal(
            kind="futures",
            title=title,
            url=None,
            sentiment="unknown",
            strength=strength,
            source_count=1,
            price_at_send=move.price_usd if move else None,
        )
        logger.info("Futures alert sent: %s", title)
        return True

    def futures_status(self) -> dict:
        """Summary for /status and /futures."""
        client = self.futures_client
        return {
            "enabled": self.futures_enabled,
            "provider": client.last_provider,
            "error": client.last_error,
            "liquidations_error": client.liquidations_error,
            "last_poll_at": self.last_futures_poll_at,
            "snapshot": self.last_futures_snapshot,
            "liquidations": self.last_liquidations,
            "history_seconds": (
                self.storage.futures_history_span(self.last_futures_snapshot.provider) if self.last_futures_snapshot else 0.0
            ),
        }

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
        self._safe(self.poll_onchain)
        self._safe(self.poll_futures)
        self._safe(self.update_signal_followups)
        return notified

    def _ensure_command_handler(self) -> None:
        if not (self.settings.enable_commands and self.notifier.is_configured):
            return
        handler = self.command_handler
        if handler is not None and handler.is_alive():
            return
        if handler is not None:
            logger.error("Command handler thread died; starting a new one")
        self.command_handler = CommandHandler(self, self.notifier)
        self.command_handler.start()

    def _start_watchdog(self) -> Optional[Watchdog]:
        hang_timeout = self.settings.watchdog_timeout_minutes * 60
        if not hang_timeout and not self.settings.max_memory_mb:
            return None
        watchdog = Watchdog(
            heartbeat=lambda: self.heartbeat_at,
            stop=self.request_restart,
            hang_timeout=hang_timeout,
            max_memory_mb=self.settings.max_memory_mb,
        )
        watchdog.start()
        return watchdog

    def _announce_start(self) -> None:
        if not self.notifier.is_configured:
            return
        username = self.notifier.get_me()
        if username:
            logger.info("Telegram bot @%s ready", username)
        elif self.notifier.last_error_code in (401, 404):
            logger.error("Telegram rejected the bot token (%s); check TELEGRAM_BOT_TOKEN", self.notifier.last_error)
        # After a few rapid restarts stay quiet: the supervisor keeps trying.
        if self.settings.send_startup_message and self.restart_count < 3:
            self.notifier.send(
                format_startup(
                    len(self.settings.rss_feeds), self.settings.keywords, self.classifier_name,
                    self.settings.enable_commands, onchain=self.onchain_enabled,
                    restart_count=self.restart_count, last_exit_code=self.last_exit_code,
                    whale_alert=self.whale_alert_enabled, futures=self.futures_enabled,
                )
            )

    def run_forever(self) -> int:
        self._install_signal_handlers()
        logger.info(
            "Starting GRAM/TON monitor: %d feeds, classifier=%s, news every %ss (%s), price every %ss, on-chain %s, futures %s",
            len(self.settings.rss_feeds), self.classifier_name,
            self.settings.poll_interval_seconds,
            f"{len(self._priority_feed_urls)} priority channel(s) every {self.settings.priority_poll_interval_seconds}s"
            if self.priority_poll_enabled else "no priority fast lane",
            self.settings.price_poll_interval_seconds,
            f"every {self.settings.onchain_poll_interval_seconds}s" if self.onchain_enabled else "disabled",
            f"every {self.settings.futures_poll_interval_seconds}s" if self.futures_enabled else "disabled",
        )
        if self.restart_count:
            logger.warning("Restarted by supervisor (%d in a row, last exit code %s)", self.restart_count, self.last_exit_code)
        self.beat()
        watchdog = self._start_watchdog()
        self._safe(self._announce_start)

        now = time.monotonic()
        next_news = now
        next_priority = float("inf")  # armed after the first full poll
        next_price = now
        next_onchain = now if self.onchain_enabled else float("inf")
        next_futures = now if self.futures_enabled else float("inf")
        next_handler_check = now
        try:
            while not self.stop_event.is_set():
                now = time.monotonic()
                if now >= next_handler_check:
                    self._safe(self._ensure_command_handler)
                    next_handler_check = now + COMMAND_HANDLER_RESTART_DELAY
                if now >= next_price:
                    self._safe(self.poll_price)
                    self._safe(self.update_signal_followups)
                    next_price = time.monotonic() + self.settings.price_poll_interval_seconds
                if now >= next_news or self._poll_requested:
                    self._poll_requested = False
                    self._safe(self.poll_news_once)
                    next_news = time.monotonic() + self.settings.poll_interval_seconds
                    if self.priority_poll_enabled:
                        # The full poll just covered the priority channels too.
                        next_priority = time.monotonic() + self.settings.priority_poll_interval_seconds
                elif now >= next_priority:
                    self._safe(self.poll_priority_once)
                    next_priority = time.monotonic() + self.settings.priority_poll_interval_seconds
                if now >= next_onchain:
                    result = self._safe(self.poll_onchain)
                    # Hit the page cap without errors: keep catching up quickly.
                    catching_up = result is not None and not result.complete and result.error is None
                    delay = 10 if catching_up else self.settings.onchain_poll_interval_seconds
                    backoff_left = max(0.0, self.ton_client.backoff_until - time.time())
                    next_onchain = time.monotonic() + max(delay, backoff_left)
                if now >= next_futures:
                    self._safe(self.poll_futures)
                    next_futures = time.monotonic() + self.settings.futures_poll_interval_seconds
                self._safe(self.maybe_prune)
                self.beat()

                timeout = max(1.0, min(next_news, next_priority, next_price, next_onchain, next_futures, next_handler_check) - time.monotonic())
                self.wake_event.wait(timeout)
                self.wake_event.clear()
        finally:
            if watchdog is not None:
                watchdog.cancel()
            if self.exit_code:
                logger.error("Monitor stopping for restart: %s (exit %d)", self.exit_reason, self.exit_code)
            else:
                logger.info("Monitor stopped")
            self.storage.close()
            self.notifier.close()
        return self.exit_code


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


def configure_logging(log_file: str = "") -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    handlers: List[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        from logging.handlers import RotatingFileHandler

        handlers.append(RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def install_crash_hooks() -> None:
    """Log fatal errors instead of dying silently (segfaults, thread crashes)."""
    try:
        faulthandler.enable()
    except (RuntimeError, AttributeError):  # pragma: no cover - no stderr
        pass

    def thread_hook(args):  # pragma: no cover - only on unexpected thread death
        logging.getLogger("grambot").critical(
            "Thread %s crashed", getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = thread_hook


def healthcheck(settings: Settings, max_age_seconds: float) -> int:
    age = heartbeat_age(settings.heartbeat_file)
    if age is None:
        print(f"no heartbeat at {settings.heartbeat_file}")
        return 1
    if age > max_age_seconds:
        print(f"heartbeat is {age:.0f}s old (limit {max_age_seconds:.0f}s)")
        return 1
    print(f"ok, heartbeat {age:.0f}s ago")
    return 0


def run(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="GRAM/TON news monitor bot")
    parser.add_argument("--once", action="store_true", help="poll price, news and chain once, then exit")
    parser.add_argument("--no-telegram", action="store_true", help="never send to Telegram (log messages instead)")
    parser.add_argument("--no-supervise", action="store_true", help="run the bot in this process without the auto-restart supervisor")
    parser.add_argument("--healthcheck", action="store_true", help="exit 0 if the running bot's heartbeat is fresh (for Docker HEALTHCHECK)")
    args = parser.parse_args(argv)
    argv_list = list(argv) if argv is not None else sys.argv[1:]

    if args.healthcheck:
        settings = Settings.from_env()
        max_age = max(15 * 60, settings.watchdog_timeout_minutes * 60 // 2 or 0)
        return healthcheck(settings, max_age)

    if not args.once and not args.no_supervise and not is_child_process():
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        return Supervisor(argv_list).run()

    settings = Settings.from_env()
    configure_logging(settings.log_file)
    install_crash_hooks()
    monitor = build_monitor(settings, telegram=not args.no_telegram)
    if args.once:
        try:
            notified = monitor.run_once()
        finally:
            monitor.storage.close()
            monitor.notifier.close()
        logger.info("Single run finished, %d notification(s)", notified)
        return 0
    return monitor.run_forever()
