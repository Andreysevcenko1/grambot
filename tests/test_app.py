import time
from unittest.mock import patch

from grambot import price as price_module
from grambot.app import build_classifier, classifier_label
from grambot.commands import MUTED_UNTIL_KEY
from grambot.config import Settings
from grambot.processing.classifier import RuleBasedClassifier
from grambot.processing.llm_classifier import LLMClassifier

from .conftest import make_item


def test_build_classifier_uses_rule_based_by_default():
    settings = Settings(openai_api_key="")
    assert isinstance(build_classifier(settings), RuleBasedClassifier)
    assert "правила" in classifier_label(settings)


def test_build_classifier_uses_llm_when_api_key_configured():
    settings = Settings(openai_api_key="fake-key", openai_model="gpt-4o-mini")
    classifier = build_classifier(settings)
    assert isinstance(classifier, LLMClassifier)
    assert classifier.model == "gpt-4o-mini"
    assert "gpt-4o-mini" in classifier_label(settings)


def test_single_source_negative_news_is_not_sent_until_corroborated(monitor):
    monitor.source.queue([make_item("TON bridge exploited, $50M drained", source="Cointelegraph")])
    assert monitor.poll_news_once() == 0
    assert monitor.notifier.sent == []

    monitor.source.queue([make_item("Hackers drain $50M from TON bridge", source="Decrypt")])
    assert monitor.poll_news_once() == 1
    message = monitor.notifier.sent[0]
    assert "негативное" in message
    assert "Источники: 2 (Cointelegraph, Decrypt)" in message

    # Third outlet on the same story must not trigger a second alert.
    monitor.source.queue([make_item("TON bridge hacked: $50M stolen", source="The Block")])
    assert monitor.poll_news_once() == 0
    assert monitor.storage.count_signals_since(0) == 1


def test_trusted_source_bypasses_corroboration(monitor):
    monitor.source.queue([make_item("Network outage: TON validators halted", source="Official Channel")])
    assert monitor.poll_news_once() == 1
    assert "Источники: 1" in monitor.notifier.sent[0]


def test_neutral_and_irrelevant_items_are_ignored(monitor):
    monitor.source.queue(
        [
            make_item("Toncoin rebrands as Gram", source="A"),
            make_item("Toncoin rebrands as Gram", source="B"),
            make_item("Ethereum upgrade shipped", source="A"),
            make_item("A 50-ton truck exploded", source="B"),
        ]
    )
    assert monitor.poll_news_once() == 0
    assert monitor.storage.count_items_since(0) == 2  # only the two relevant items were stored


def test_stale_items_are_dropped(monitor):
    monitor.source.queue([make_item("TON exploit confirmed", source="A", minutes_ago=48 * 60)])
    monitor.poll_news_once()
    assert monitor.storage.count_items_since(0) == 0


def test_seen_items_are_not_reprocessed(monitor):
    item = make_item("Binance delists TON", source="A")
    monitor.source.queue([item])
    monitor.source.queue([item, make_item("Binance delists TON", source="B")])
    monitor.poll_news_once()
    monitor.poll_news_once()
    assert monitor.storage.cluster_source_count(monitor.storage.recent_clusters(0)[0].cluster_id) == 2
    assert len(monitor.notifier.sent) == 1


def test_mute_and_rate_limit_suppress_notifications(monitor):
    monitor.storage.set_value(MUTED_UNTIL_KEY, str(time.time() + 600))
    assert monitor.can_notify() == (False, "muted")
    monitor.source.queue([make_item("Binance delists TON", source="A"), make_item("Binance delists TON", source="B")])
    assert monitor.poll_news_once() == 0
    monitor.storage.set_value(MUTED_UNTIL_KEY, None)

    for i in range(3):
        monitor.storage.record_signal("news", f"s{i}", None, "negative", "high", 2, price_at_send=1.0)
    ok, why = monitor.can_notify()
    assert not ok and "rate limit" in why


def test_failed_send_does_not_mark_cluster_notified(monitor):
    monitor.notifier.fail = True
    monitor.source.queue([make_item("Binance delists TON", source="A"), make_item("Binance delists TON", source="B")])
    assert monitor.poll_news_once() == 0
    cluster = monitor.storage.recent_clusters(0)[0]
    assert not cluster.notified

    monitor.notifier.fail = False
    monitor.source.queue([make_item("Binance to delist TON", source="C")])
    assert monitor.poll_news_once() == 1


def test_price_alert_with_cooldown(monitor):
    now = time.time()
    monitor.storage.add_price_point(2.0, 1000.0, fetched_at=now - 30 * 60)
    snap = price_module.PriceSnapshot(price_usd=2.2, volume_24h_usd=1500.0, change_24h_pct=8.0, fetched_at=now)
    with patch.object(price_module.PriceClient, "fetch", return_value=snap):
        move = monitor.poll_price()
        assert move.window_change_pct > 5
        assert len(monitor.notifier.sent) == 1
        assert "Резкий рост" in monitor.notifier.sent[0]
        # Same spike again: cooldown prevents a duplicate alert.
        monitor.poll_price()
        assert len(monitor.notifier.sent) == 1
    assert monitor.storage.count_signals_since(0) == 1


def test_news_alert_includes_fresh_price_context(monitor):
    now = time.time()
    monitor.storage.add_price_point(2.0, 1000.0, fetched_at=now - 30 * 60)
    monitor.storage.add_price_point(1.9, 1200.0, change_24h_pct=-3.0, fetched_at=now - 10)
    monitor.source.queue([make_item("Binance delists TON", source="A"), make_item("Binance delists TON", source="B")])
    with patch.object(price_module.PriceClient, "fetch") as fetch:
        monitor.poll_news_once()
        fetch.assert_not_called()  # recent stored point is reused
    assert "−5,0% за 20 мин" in monitor.notifier.sent[0]
    assert "−3,0% за 24ч" in monitor.notifier.sent[0]


def test_signal_followups_are_filled_from_history(monitor):
    now = time.time()
    monitor.storage.record_signal("news", "t", None, "negative", "high", 2, price_at_send=2.0, sent_at=now - 3700)
    monitor.storage.add_price_point(1.8, 1.0, fetched_at=now - 100)
    assert monitor.update_signal_followups() == 1
    stats = monitor.storage.signal_stats()
    assert stats.hits_1h == 1


def test_run_once_and_prune(monitor):
    monitor.source.queue([])
    with patch.object(price_module.PriceClient, "fetch", return_value=None):
        assert monitor.run_once() == 0
    monitor.maybe_prune()
    assert monitor.storage.get_float("last_prune_at") is not None
