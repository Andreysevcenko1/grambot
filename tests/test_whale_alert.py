import time

from grambot.app import LAST_WHALE_ALERT_KEY, WHALE_ALERT_MAX_AGE_SECONDS
from grambot.processing.whale_alert import is_transfer_post, is_whale_alert_item, parse_transfer

from .conftest import make_item

WA_SOURCE = "Whale Alert - Telegram Channel"


def wa_item(text, minutes_ago=2, post_id="12345"):
    return make_item(text, source=WA_SOURCE, minutes_ago=minutes_ago, url=f"https://t.me/whale_alert_io/{post_id}")


def test_parse_exchange_deposit():
    item = wa_item("🚨 🚨 🚨 12,000,000 $TON (30,120,000 USD) transferred from unknown wallet to #Binance Details")
    transfer = parse_transfer(item)
    assert transfer is not None
    assert transfer.amount_ton == 12_000_000
    assert transfer.usd_value == 30_120_000
    assert transfer.kind == "exchange_deposit"
    assert transfer.source_label is None
    assert transfer.destination_label.name == "Binance" and transfer.destination_label.category == "CEX"
    assert transfer.hash == "whale-alert:12345"
    assert transfer.url == "https://t.me/whale_alert_io/12345"
    assert transfer.source_friendly == "" and transfer.destination_friendly == ""


def test_parse_withdrawal_between_exchange_and_wallet_and_gram_symbol():
    item = wa_item("🚨 3,500,000 $GRAM (8,750,000 USD) transferred from #OKX to unknown wallet Details")
    transfer = parse_transfer(item)
    assert transfer is not None and transfer.kind == "exchange_withdrawal"
    assert transfer.source_label.is_exchange and transfer.destination_label is None

    item = wa_item("🚨 2,000,000 $TON (5,000,000 USD) transferred from #Bybit to #Binance Details")
    assert parse_transfer(item).kind == "exchange_to_exchange"

    item = wa_item("🔥 1,000,000 $TON (2,500,000 USD) burned at unknown wallet")
    burned = parse_transfer(item)
    assert burned is not None and burned.kind == "fund"


def test_other_coins_and_news_posts_are_not_ton_transfers():
    usdc = wa_item("🚨 100,000,000 $USDC (100,000,000 USD) transferred from #Circle to unknown wallet Details")
    assert parse_transfer(usdc) is None
    assert is_transfer_post(usdc)  # still a transfer line: keep it out of the news pipeline
    news = wa_item("📉 📉 📉 Bitcoin drops below $60k as liquidations mount Read Analysis")
    assert parse_transfer(news) is None
    assert not is_transfer_post(news)
    assert is_whale_alert_item(news)
    assert not is_whale_alert_item(make_item("TON news", source="Cointelegraph"))


def test_whale_alert_post_triggers_whale_alert_and_feeds_whales_command(monitor):
    monitor.onchain_enabled = False
    monitor.whale_alert_enabled = True
    monitor.source.queue([
        wa_item("🚨 🚨 🚨 12,000,000 $TON (30,120,000 USD) transferred from unknown wallet to #Binance Details", post_id="777"),
        wa_item("🚨 100,000,000 $USDC (100,000,000 USD) transferred from #Circle to unknown wallet Details", post_id="778"),
        wa_item("📉 📉 📉 TON falls 8% amid market selloff Read Analysis", post_id="779"),
    ])
    monitor.poll_news_once()
    whale = [m for m in monitor.notifier.sent if "Крупный перевод" in m]
    assert len(whale) == 1
    assert "12,00 млн TON" in whale[0] and "$30,1 млн" in whale[0]
    assert "<b>Binance</b>" in whale[0] and "депозит на биржу" in whale[0]
    assert 'href="https://t.me/whale_alert_io/777">Whale Alert</a>' in whale[0]
    # The USDC line never became a news item; the headline did (unverified, single source).
    assert monitor.storage.count_items_since(time.time() - 3600) == 1
    assert not [m for m in monitor.notifier.sent if "USDC" in m or "market selloff" in m]
    assert monitor.storage.count_signals_since(time.time() - 3600) == 1

    from grambot.commands import CommandHandler

    text = CommandHandler(monitor, monitor.notifier).handle_command("/whales")
    assert "Крупные переводы за 24ч" in text and "Переводов: 1" in text
    assert 'href="https://t.me/whale_alert_io/777"' in text and "Whale Alert" in text

    # The same post seen again is a no-op (deduplicated by post id).
    monitor.source.queue([
        wa_item("🚨 🚨 🚨 12,000,000 $TON (30,120,000 USD) transferred from unknown wallet to #Binance Details", post_id="777"),
    ])
    monitor.poll_news_once()
    assert len([m for m in monitor.notifier.sent if "Крупный перевод" in m]) == 1


def test_old_or_small_whale_alert_posts_are_recorded_but_not_alerted(monitor):
    monitor.whale_alert_enabled = True
    old_minutes = WHALE_ALERT_MAX_AGE_SECONDS // 60 + 10
    monitor.source.queue([
        wa_item("🚨 9,000,000 $TON (22,000,000 USD) transferred from #Binance to unknown wallet Details", minutes_ago=old_minutes, post_id="1"),
        wa_item("🚨 100,000 $TON (250,000 USD) transferred from #Binance to unknown wallet Details", post_id="2"),
    ])
    monitor.poll_news_once()
    assert not [m for m in monitor.notifier.sent if "Крупный перевод" in m]
    assert monitor.storage.get_float(LAST_WHALE_ALERT_KEY) is None
    assert monitor.storage.flow_stats(time.time() - 86400).total_count == 2
