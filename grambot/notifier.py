"""Formats and sends notifications to a Telegram chat via the Bot API."""
from __future__ import annotations

import logging
from typing import Optional

import requests

from .price import PriceMove
from .processing.classifier import Classification
from .sources import NewsItem

logger = logging.getLogger(__name__)

SENTIMENT_EMOJI = {
    "positive": "🟢",
    "negative": "🔴",
    "neutral": "⚪",
    "unknown": "🟡",
}

SENTIMENT_LABEL_RU = {
    "positive": "Возможное позитивное влияние",
    "negative": "Возможное негативное влияние",
    "neutral": "Нейтральная новость",
    "unknown": "Требует проверки",
}

STRENGTH_LABEL_RU = {
    "low": "низкая",
    "medium": "средняя",
    "high": "высокая",
}


def format_price_line(move: Optional[PriceMove]) -> str:
    if move is None:
        return "TON: данные о цене недоступны"
    if move.price_change_pct is None:
        return f"TON: ${move.price_usd:,.4f} (недостаточно истории для расчёта движения)"
    direction = "+" if move.price_change_pct >= 0 else ""
    volume_part = (
        f", объём вырос в {move.volume_ratio:.1f}x" if move.volume_ratio else ""
    )
    return (
        f"TON: {direction}{move.price_change_pct:.1f}% за {move.window_minutes} мин"
        f"{volume_part}"
    )


def format_message(
    item: NewsItem,
    classification: Classification,
    source_count: int,
    verified: bool,
    price_move: Optional[PriceMove],
) -> str:
    emoji = SENTIMENT_EMOJI.get(classification.sentiment, "⚪")
    label = SENTIMENT_LABEL_RU.get(classification.sentiment, classification.sentiment)
    strength_label = STRENGTH_LABEL_RU.get(classification.strength, classification.strength)
    verification = "подтверждено" if verified else "непроверено, требует подтверждения"

    lines = [
        f"{emoji} {label}",
        "",
        item.title,
        "",
        f"Сила сигнала: {strength_label}",
        f"Источники: {source_count} ({verification})",
        format_price_line(price_move),
    ]
    if item.url:
        lines.append(f"Оригинал: {item.url}")
    return "\n".join(lines)


class TelegramNotifier:
    """Sends messages via the Telegram Bot API, or logs them in dry-run mode."""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id

    @property
    def is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send(self, text: str) -> bool:
        if not self.is_configured:
            logger.info("[DRY RUN] Would send Telegram message:\n%s", text)
            return True

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            response = requests.post(
                url,
                data={
                    "chat_id": self.chat_id,
                    "text": text,
                    "disable_web_page_preview": False,
                },
                timeout=10,
            )
            response.raise_for_status()
            return True
        except Exception:  # pragma: no cover - defensive, network dependent
            logger.exception("Failed to send Telegram notification")
            return False
