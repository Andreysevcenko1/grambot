from grambot.notifier import (
    TelegramNotifier,
    fmt_pct,
    fmt_usd,
    format_news_alert,
    format_price_alert,
    format_startup,
    truncate,
)
from grambot.price import PriceMove
from grambot.processing.classifier import Classification
from grambot.sources import NewsItem


def make_move(**overrides):
    base = dict(
        price_usd=5.0,
        window_minutes=20,
        window_change_pct=-4.2,
        change_24h_pct=-6.15,
        volume_24h_usd=310_000_000.0,
        volume_ratio_vs_yesterday=2.1,
    )
    base.update(overrides)
    return PriceMove(**base)


def test_format_news_alert_includes_key_fields():
    item = NewsItem(
        source="Cointelegraph",
        title="TON Foundation announced a <network> problem",
        summary="details",
        url="https://example.com/news/1?a=1&b=2",
        published_at=0.0,
    )
    classification = Classification(sentiment="negative", strength="high", matched_keywords=["outage"], reason="outage, TON Foundation")
    message = format_news_alert(
        item, classification, source_count=3, sources=["Cointelegraph", "Decrypt", "The Block"], price_move=make_move()
    )

    assert message.startswith("🔴 Возможное негативное влияние")
    assert "&lt;network&gt;" in message  # HTML-escaped title
    assert "Сила: высокая" in message
    assert "Источники: 3 (Cointelegraph, Decrypt, The Block)" in message
    assert "Причина: outage, TON Foundation" in message
    assert "−4,2% за 20 мин" in message
    assert "−6,2% за 24ч" in message
    assert "$310,0 млн" in message
    assert "×2,1 к вчера" in message
    assert 'href="https://example.com/news/1?a=1&amp;b=2"' in message
    assert "не финансовая рекомендация" in message


def test_format_news_alert_marks_unverified_and_unknown():
    item = NewsItem(source="X", title="Rumor", summary="", url=None, published_at=0.0)
    message = format_news_alert(item, Classification("unknown", "low"), source_count=1, verified=False)
    assert "Требует проверки" in message
    assert "не подтверждено" in message
    assert "Оригинал" not in message


def test_format_price_alert():
    message = format_price_alert(make_move(window_change_pct=6.3))
    assert message.startswith("📈 Резкий рост TON: +6,3% за 20 мин")
    down = format_price_alert(make_move(window_change_pct=-7.0))
    assert down.startswith("📉 Резкое падение")


def test_format_startup_lists_commands_when_enabled():
    text = format_startup(9, ["TON"], "правила", commands_enabled=True)
    assert "Лент: 9" in text and "/status" in text
    assert "/status" not in format_startup(9, ["TON"], "правила", commands_enabled=False)


def test_formatting_helpers():
    assert fmt_pct(None) == "н/д"
    assert fmt_pct(0.0) == "0,0%"
    assert fmt_pct(12.345, 2) == "+12,35%"
    assert fmt_usd(1_500_000_000) == "$1,50 млрд"
    assert fmt_usd(12_345) == "$12 345"
    assert truncate("x" * 5000).endswith("…")
    assert len(truncate("x" * 5000)) == 4096


def test_notifier_dry_run_when_not_configured():
    notifier = TelegramNotifier("", "")
    assert not notifier.is_configured
    assert notifier.send("hello") is True
    assert notifier.get_updates(None) == []
