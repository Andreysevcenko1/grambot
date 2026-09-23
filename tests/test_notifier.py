from grambot.notifier import TelegramNotifier, format_message
from grambot.price import PriceMove
from grambot.processing.classifier import Classification
from grambot.sources import NewsItem


def test_format_message_includes_key_fields():
    item = NewsItem(
        source="ton-blog",
        title="TON Foundation announced a network problem",
        summary="details",
        url="https://example.com/news/1",
        published_at=0.0,
    )
    classification = Classification(sentiment="negative", strength="high", matched_keywords=["network"])
    price_move = PriceMove(
        price_usd=5.0,
        volume_usd=1000.0,
        price_change_pct=-4.2,
        volume_ratio=2.1,
        window_minutes=20,
    )
    message = format_message(item, classification, source_count=3, verified=True, price_move=price_move)

    assert "Возможное негативное влияние" in message
    assert "TON Foundation announced a network problem" in message
    assert "высокая" in message
    assert "Источники: 3" in message
    assert "-4.2%" in message
    assert "2.1x" in message
    assert "https://example.com/news/1" in message


def test_notifier_dry_run_when_not_configured(caplog):
    notifier = TelegramNotifier("", "")
    assert not notifier.is_configured
    assert notifier.send("hello") is True
