import time
from unittest.mock import patch

from grambot import price as price_module
from grambot.commands import CommandHandler, MUTED_UNTIL_KEY

from .conftest import make_item


def handler_for(monitor):
    return CommandHandler(monitor, monitor.notifier)


def test_unauthorized_chat_is_ignored(monitor):
    handler = handler_for(monitor)
    handler.handle_update({"update_id": 1, "message": {"chat": {"id": 999}, "text": "/status"}})
    assert monitor.notifier.sent_to == []


def test_status_and_help(monitor):
    handler = handler_for(monitor)
    handler.handle_update({"update_id": 1, "message": {"chat": {"id": 1}, "text": "/status@Tongramcheckerbot"}})
    assert monitor.notifier.sent_to[0][0] == "1"
    assert "Состояние" in monitor.notifier.sent_to[0][1]
    assert "/mute" in handler.handle_command("/help")
    assert "Неизвестная" in handler.handle_command("/foo")


def test_mute_unmute(monitor):
    handler = handler_for(monitor)
    reply = handler.handle_command("/mute 30")
    assert "30 мин" in reply
    assert monitor.muted_until() is not None
    assert "включены" in handler.handle_command("/unmute")
    assert monitor.storage.get_value(MUTED_UNTIL_KEY) is None
    handler.handle_command("/mute")
    assert "включены" in handler.handle_command("/mute off")


def test_recent_and_feeds_and_stats(monitor):
    handler = handler_for(monitor)
    assert "пока нет" in handler.handle_command("/recent")
    assert "ещё не опрашивались" in handler.handle_command("/feeds")

    monitor.source.queue([make_item("Binance delists TON", source="A", url="https://a.example/1")])
    monitor.poll_news_once()
    recent = handler.handle_command("/recent 1")
    assert "Binance delists TON" in recent and "https://a.example/1" in recent
    feeds = handler.handle_command("/feeds")
    assert "✅ Fake" in feeds

    stats = handler.handle_command("/stats")
    assert "Новостных сигналов: 0" in stats


def test_check_requests_poll(monitor):
    handler = handler_for(monitor)
    assert "Проверяю" in handler.handle_command("/check")
    assert monitor.wake_event.is_set()
    assert monitor._poll_requested


def test_test_command_renders_alert(monitor):
    handler = handler_for(monitor)
    text = handler.handle_command("/test")
    assert "ТЕСТ" in text and "Сила: высокая" in text


def test_price_command_falls_back_to_history(monitor):
    handler = handler_for(monitor)
    with patch.object(price_module.PriceClient, "fetch", return_value=None):
        assert "недоступны" in handler.handle_command("/price")
        monitor.storage.add_price_point(2.5, 100.0, change_24h_pct=1.0, fetched_at=time.time())
        assert "$2,500" in handler.handle_command("/price")


def test_price_command_shows_local_currency_and_provider(monitor):
    monitor.settings.display_currency = "EUR"
    handler = handler_for(monitor)
    monitor.storage.add_price_point(2.0, 100.0, change_24h_pct=1.0, fetched_at=time.time())
    text = handler.handle_command("/price")
    assert "$2,000 (≈ 1,800 €)" in text
    assert "Источник:" in text


def test_whales_command_empty_and_with_data(monitor):
    handler = handler_for(monitor)
    text = handler.handle_command("/whales")
    assert "Ончейн за 24ч" in text and "не зафиксировано" in text
    assert "Ончейн: ожидает первого опроса" in handler.handle_command("/status")

    now = time.time()
    monitor.storage.record_transfer("h1", now - 60, "0:A", "0:B", 600_000, None, "Binance (hot wallet)", "exchange_deposit")
    monitor.storage.record_transfer("h2", now - 30, "0:C", "0:D", 200_000, "OKX", None, "exchange_withdrawal")
    monitor.storage.record_transfer("h3", now - 30, "0:E", "0:F", 70_000, None, None, "unknown")
    monitor.storage.mark_transfer_notified("h1")
    monitor.storage.set_value("onchain_last_utime", str(now - 20))
    monitor.last_onchain_poll_at = now
    text = handler.handle_command("/whales 2")
    assert "Переводов ≥ 50 тыс. TON: 3" in text
    assert "На биржи: 600 тыс. TON (1)" in text and "с бирж: 200 тыс. TON (1)" in text
    assert "приток на биржи" in text
    assert "Крупнейшие 2" in text and "h1" in text and "h3" not in text
    assert "Binance (hot wallet)" in text and "🔔" in text
    assert "отставание" in text
    assert "Ончейн: отставание" in handler.handle_command("/status")

    monitor.onchain_enabled = False
    assert "выключен" in handler.handle_command("/whales")
    assert "Ончейн: выключен" in handler.handle_command("/status")
