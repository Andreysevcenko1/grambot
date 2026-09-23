"""Telegram notifications: message formatting (Russian, HTML) and the
Bot API client used for both alerts and command handling."""
from __future__ import annotations

import html
import logging
import re
import time
from typing import Any, Dict, List, Optional, Sequence

import requests

from .price import PriceMove
from .processing.classifier import Classification
from .sources import NewsItem

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE_LENGTH = 4096
_TAG_RE = re.compile(r"<[^>]+>")

SENTIMENT_HEADERS = {
    "negative": "🔴 Возможное негативное влияние",
    "positive": "🟢 Возможное позитивное влияние",
    "unknown": "⚪ Требует проверки: влияние неясно",
    "neutral": "⚪ Нейтральная новость",
}
STRENGTH_LABELS = {"low": "низкая", "medium": "средняя", "high": "высокая"}
DISCLAIMER = "ℹ️ Информационный сигнал, не финансовая рекомендация."
CURRENCY_SYMBOLS = {
    "USD": "$", "EUR": "€", "RUB": "₽", "UAH": "₴", "KZT": "₸", "GBP": "£",
    "BYN": "Br", "TRY": "₺", "PLN": "zł", "CZK": "Kč", "GEL": "₾", "AMD": "֏",
    "JPY": "¥", "CNY": "¥", "KRW": "₩", "INR": "₹", "BRL": "R$", "ILS": "₪",
}


def fmt_pct(value: Optional[float], digits: int = 1) -> str:
    if value is None:
        return "н/д"
    sign = "+" if value > 0 else ("−" if value < 0 else "")
    return f"{sign}{abs(value):.{digits}f}%".replace(".", ",")


def fmt_usd(value: Optional[float]) -> str:
    if value is None:
        return "н/д"
    if value >= 1e9:
        return f"${value / 1e9:.2f} млрд".replace(".", ",")
    if value >= 1e6:
        return f"${value / 1e6:.1f} млн".replace(".", ",")
    if value >= 1e3:
        return f"${value:,.0f}".replace(",", " ")
    return f"${value:.4f}".replace(".", ",")


def fmt_price(value: float, currency: str = "USD") -> str:
    digits = 4 if value < 1 else (3 if value < 10 else 2)
    number = f"{value:.{digits}f}".replace(".", ",")
    symbol = CURRENCY_SYMBOLS.get(currency.upper())
    if currency.upper() == "USD":
        return f"${number}"
    if symbol:
        return f"{number} {symbol}"
    return f"{number} {currency.upper()}"


def fmt_price_move(move: PriceMove) -> str:
    """USD price plus the display-currency equivalent when configured."""
    text = fmt_price(move.price_usd)
    if move.local_price is not None and move.local_currency.upper() != "USD":
        text += f" (≈ {fmt_price(move.local_price, move.local_currency)})"
    return text


def fmt_ratio(value: Optional[float]) -> str:
    if value is None:
        return "н/д"
    return f"×{value:.1f}".replace(".", ",")


def fmt_time(ts: float) -> str:
    return time.strftime("%d.%m %H:%M", time.localtime(ts))


def truncate(text: str, limit: int = MAX_MESSAGE_LENGTH) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def format_price_context(move: Optional[PriceMove]) -> List[str]:
    if move is None:
        return []
    parts = [f"TON: {fmt_price_move(move)}"]
    if move.window_change_pct is not None:
        parts.append(f"{fmt_pct(move.window_change_pct)} за {move.window_minutes} мин")
    if move.change_24h_pct is not None:
        parts.append(f"{fmt_pct(move.change_24h_pct)} за 24ч")
    lines = [" · ".join(parts)]
    if move.volume_24h_usd:
        volume = f"Объём 24ч: {fmt_usd(move.volume_24h_usd)}"
        if move.volume_ratio_vs_yesterday is not None:
            volume += f" ({fmt_ratio(move.volume_ratio_vs_yesterday)} к вчера)"
        lines.append(volume)
    return lines


def format_news_alert(
    item: NewsItem,
    classification: Classification,
    source_count: int,
    sources: Sequence[str] = (),
    verified: bool = True,
    price_move: Optional[PriceMove] = None,
) -> str:
    header = SENTIMENT_HEADERS.get(classification.sentiment, SENTIMENT_HEADERS["unknown"])
    strength = STRENGTH_LABELS.get(classification.strength, classification.strength)

    lines = [header, f"<b>{html.escape(item.title)}</b>", ""]
    lines.append(f"Сила: {strength}")
    source_line = f"Источники: {source_count}"
    if sources:
        names = ", ".join(html.escape(s) for s in list(sources)[:4])
        if len(sources) > 4:
            names += "…"
        source_line += f" ({names})"
    if not verified:
        source_line += " — не подтверждено"
    lines.append(source_line)
    if classification.reason:
        lines.append(f"Причина: {html.escape(classification.reason)}")
    if item.summary:
        summary = item.summary if len(item.summary) <= 300 else item.summary[:297].rstrip() + "…"
        lines.extend(["", f"<i>{html.escape(summary)}</i>"])

    price_lines = format_price_context(price_move)
    if price_lines:
        lines.append("")
        lines.extend(price_lines)

    lines.append("")
    if item.url:
        lines.append(f'🔗 <a href="{html.escape(item.url, quote=True)}">Оригинал</a> · {fmt_time(item.published_at)}')
    lines.append(DISCLAIMER)
    return truncate("\n".join(lines))


def format_price_alert(move: PriceMove) -> str:
    change = move.window_change_pct or 0.0
    emoji = "📈" if change > 0 else "📉"
    direction = "Резкий рост" if change > 0 else "Резкое падение"
    lines = [
        f"{emoji} {direction} TON: {fmt_pct(change)} за {move.window_minutes} мин",
        "",
    ]
    lines.extend(format_price_context(move))
    lines.extend(["", "Новостей, объясняющих движение, пока не найдено — проверьте источники.", DISCLAIMER])
    return truncate("\n".join(lines))


def format_startup(feed_count: int, keywords: Sequence[str], classifier_name: str, commands_enabled: bool) -> str:
    lines = [
        "🤖 GRAM/TON монитор запущен",
        f"Лент: {feed_count} · ключевых слов: {len(keywords)}",
        f"Классификатор: {html.escape(classifier_name)}",
    ]
    if commands_enabled:
        lines.append("Команды: /status /price /recent /stats /feeds /help")
    return "\n".join(lines)


class TelegramNotifier:
    """Minimal Telegram Bot API client (HTML parse mode)."""

    def __init__(self, token: str, chat_id: str, timeout: float = 15.0):
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout
        self._session = requests.Session()

    @property
    def is_configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def _call(self, method: str, request_timeout: Optional[float] = None, **params: Any) -> Optional[Dict[str, Any]]:
        if not self.token:
            return None
        url = TELEGRAM_API.format(token=self.token, method=method)
        try:
            response = self._session.post(url, json=params, timeout=request_timeout or self.timeout)
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Telegram %s failed: %s", method, exc)
            return None
        if not payload.get("ok"):
            logger.warning("Telegram %s error: %s", method, payload.get("description"))
            return None
        return payload

    def send_to(self, chat_id: str, text: str, disable_preview: bool = True) -> bool:
        text = truncate(text)
        if not self.token or not chat_id:
            logger.info("[dry-run] Telegram message:\n%s", text)
            return True
        payload = self._call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=disable_preview,
        )
        if payload is None:
            # Retry once as plain text in case the HTML markup was rejected.
            payload = self._call("sendMessage", chat_id=chat_id, text=html.unescape(_TAG_RE.sub("", text)))
        return payload is not None

    def send(self, text: str, disable_preview: bool = True) -> bool:
        return self.send_to(self.chat_id, text, disable_preview=disable_preview)

    def get_updates(self, offset: Optional[int], timeout_seconds: int = 20) -> Optional[List[Dict[str, Any]]]:
        """Long-poll for updates. Returns ``None`` on error (vs. ``[]`` when quiet)."""
        if not self.token:
            return []
        params: Dict[str, Any] = {"timeout": timeout_seconds, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        payload = self._call("getUpdates", request_timeout=timeout_seconds + 10, **params)
        if payload is None:
            return None
        return list(payload.get("result", []))

    def set_commands(self, commands: Sequence["tuple[str, str]"]) -> bool:
        payload = self._call(
            "setMyCommands",
            commands=[{"command": name, "description": desc} for name, desc in commands],
        )
        return payload is not None

    def close(self) -> None:
        self._session.close()
