import time
from unittest.mock import Mock, patch

import pytest

from grambot.app import GramTonMonitor
from grambot.commands import CommandHandler
from grambot.config import Settings
from grambot.processing.classifier import RuleBasedClassifier
from grambot.sources import NewsItem
from grambot.sources.listings import fetch_listings, listing_label
from grambot.sources.rss import FeedResult
from grambot.watchlist import BINANCE_FEED, UPBIT_FEED, active_people, match_people


@pytest.fixture
def watch(monitor):
    settings = Settings(news_mode="watchlist", watchlist_aggregators=["https://example.com/feed"])
    return GramTonMonitor(settings, monitor.storage, monitor.source, RuleBasedClassifier(), monitor.notifier)


def item(title, source="News", minutes=0, url="https://example.com/1"):
    return NewsItem(source, title, "", url, time.time() - minutes * 60)


def test_people_require_relevant_context_and_identified_source():
    people = active_people([])
    assert [p.key for p in match_people("Durov announces TON wallet", "https://example.com", people)] == ["durov"]
    assert not match_people("Clark covers a TON of ground", "https://example.com", people)
    assert not match_people("Elon Musk launches a rocket", "https://example.com", people)
    assert [p.key for p in match_people("A TON wallet", "https://t.me/s/durov", people)] == ["durov"]
    assert not match_people("TON wallet", "https://example.com", people)
    assert not match_people("Trump campaign", "https://trumpstruth.org/feed", people)
    assert [p.key for p in match_people("Bitcoin reserve", "https://trumpstruth.org/feed", people)] == ["trump"]
    assert not match_people("Elon Musk discusses DOGE", "https://example.com", active_people(["musk"]))
    with pytest.raises(ValueError):
        active_people(["typo"])


@pytest.mark.parametrize("title,exchange,expected", [
    ("Binance Will List Predict (PREDICT) with Seed Tag Applied", "Binance", "PREDICT"),
    ("Predict (PREDICT) Listing Will Be Postponed", "Binance", None),
    ("콜렉터크립트(CARDS) 신규 거래지원 안내 (KRW, BTC, USDT 마켓)", "Upbit", "CARDS"),
    ("블러(BLUR) 거래지원 종료 안내", "Upbit", None),
    ("닐리온(NIL) KRW 마켓 디지털 자산 추가", "Upbit", "NIL"),
    ("OKX to list SLX/USDT (Solstice) for spot trading", "OKX", "SLX"),
    ("OKX to support VELODROME crypto migration", "OKX", None),
])
def test_listing_detection(title, exchange, expected):
    assert listing_label(title, exchange) == expected


def test_upbit_and_okx_api_parsing_and_errors():
    response = Mock()
    session = Mock()
    session.get.return_value = response
    response.json.return_value = {"data": {"notices": [
        {"id": 123, "title": "신규 거래지원 (TEST)", "first_listed_at": "2026-01-01T10:00:00+09:00"}
    ]}}
    result = fetch_listings(UPBIT_FEED, "Upbit", 2, session)
    assert result.ok and result.items[0].url.endswith("id=123")
    response.json.return_value = {"code": "0", "data": [{"details": [
        {"title": "OKX to list TEST/USDT for spot trading", "url": "https://okx.com/test", "pTime": "1767225600000"}
    ]}]}
    result = fetch_listings("https://example.com/okx", "OKX", 2, session)
    assert result.ok and result.items[0].published_at == 1767225600
    response.json.return_value = {"code": "500", "msg": "unavailable"}
    assert not fetch_listings("https://example.com/okx", "OKX", 2, session).ok


def test_watch_alerts_provenance_dedup_and_no_classification(watch):
    feed = "https://example.com/feed"
    first = item("Durov announces TON wallet", url="https://example.com/one")
    duplicate = item(first.title, source="Other", url="https://example.com/two")
    results = [FeedResult(feed, True, [first, duplicate])]
    assert watch._process_feed_results(results, "Poll")[0] == 1
    msg = watch.notifier.sent[-1]
    assert "Павел Дуров → TON" in msg and "Сообщение СМИ, требует проверки" in msg
    assert "Сила:" not in msg and "Возможная причина" not in msg
    assert watch._process_feed_results(results, "Poll")[0] == 0
    assert watch.storage.count_watch_signals() == 1


def test_failed_send_is_retried_and_old_items_ignored(watch):
    post = item("Bitcoin reserve", url="https://example.com/trump")
    old = item("Elon Musk discusses DOGE", minutes=16, url="https://example.com/old")
    results = [FeedResult("https://trumpstruth.org/feed", True, [post]),
               FeedResult("https://example.com/feed", True, [old])]
    watch.notifier.fail = True
    assert watch._process_feed_results(results, "Poll")[0] == 0
    assert not watch.storage.has_seen(post.item_hash)
    watch.notifier.fail = False
    assert watch._process_feed_results(results, "Poll")[0] == 1
    assert "Неофициальная копия поста" in watch.notifier.sent[-1]
    assert not watch.storage.has_seen(old.item_hash)


def test_listing_and_mute(watch):
    post = item("Binance Will List Predict (PREDICT)", url="https://t.me/binance_announcements/123")
    result = [FeedResult(BINANCE_FEED, True, [post])]
    watch.storage.set_value("muted_until", str(time.time() + 300))
    assert watch._process_feed_results(result, "Poll")[0] == 0
    watch.storage.set_value("muted_until", None)
    assert watch._process_feed_results(result, "Poll")[0] == 0


def test_watch_mode_disables_market_and_commands(watch):
    assert not watch.onchain_enabled and not watch.futures_enabled and not watch.whale_alert_enabled
    handler = CommandHandler(watch, watch.notifier)
    assert "выключен" in handler.cmd_price([])
    assert "Павел Дуров → TON" in handler.cmd_watchlist([])
    assert "выключены" in handler.cmd_status([])
    with patch.object(watch, "poll_price") as price, patch.object(watch, "poll_news_once", return_value=0):
        watch.run_once()
        price.assert_not_called()


def test_watch_commands_exclude_legacy_history(watch):
    watch.storage.record_signal("news", "old classic headline", None, None, None, 1, None)
    handler = CommandHandler(watch, watch.notifier)
    assert "Сигналов за 24ч: 0" in handler.cmd_status([])
    assert "пока нет" in handler.cmd_recent([])


def test_item_failure_does_not_block_other_items(watch):
    first = item("Durov announces TON wallet", url="https://example.com/1")
    second = item("Elon Musk discusses DOGE", url="https://example.com/2")
    original = watch._process_watch_item

    def process(post, feed):
        if post is first:
            raise ValueError("bad item")
        return original(post, feed)

    with patch.object(watch, "_process_watch_item", side_effect=process):
        assert watch._process_watch_results([FeedResult("https://example.com/feed", True, [first, second])]) == 1


def test_default_config_is_watchlist(monkeypatch):
    monkeypatch.delenv("NEWS_MODE", raising=False)
    monkeypatch.delenv("POLL_INTERVAL_SECONDS", raising=False)
    settings = Settings.from_env()
    assert settings.news_mode == "watchlist"
    assert settings.poll_interval_seconds == 60
