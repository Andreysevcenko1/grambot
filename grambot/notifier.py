"""Telegram notifications: message formatting (Russian, HTML) and the
Bot API client used for both alerts and command handling."""
from __future__ import annotations

import html
import logging
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

from .derivatives import FuturesSnapshot, LiquidationStats, funding_note, positioning_note
from .onchain import KIND_LABELS, Transfer
from .price import PriceMove
from .processing.classifier import Classification
from .sources import NewsItem

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE_LENGTH = 4096
MAX_FLOOD_WAIT_SECONDS = 30.0
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


def fmt_times(value: float) -> str:
    """Multiplier for prose: ``в 4,2 раза``, ``в 3 раза``, ``в 5 раз``."""
    if abs(value - round(value)) < 0.05:
        n = int(round(value))
        word = "раза" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "раз"
        return f"в {n} {word}"
    return f"в {value:.1f} раза".replace(".", ",")


def fmt_window(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} мин"
    if minutes % 1440 == 0 and minutes > 1440:
        return f"{minutes // 1440} д"
    if minutes % 60 == 0:
        return f"{minutes // 60} ч"
    return f"{minutes / 60:.1f} ч".replace(".", ",")


def fmt_time(ts: float) -> str:
    return time.strftime("%d.%m %H:%M", time.localtime(ts))


def truncate(text: str, limit: int = MAX_MESSAGE_LENGTH) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _window_line(move: PriceMove) -> Optional[str]:
    """``−1,2% за 20 мин · +3,6% за 1 ч · +6,1% за 4 ч`` (24h is shown on the headline line)."""
    parts = []
    for minutes in sorted(move.window_changes):
        change = move.window_changes[minutes]
        if change is None or (minutes == 1440 and move.change_24h_pct is not None):
            continue
        parts.append(f"{fmt_pct(change)} за {fmt_window(minutes)}")
    if not parts and move.window_change_pct is not None:
        parts.append(f"{fmt_pct(move.window_change_pct)} за {fmt_window(move.window_minutes)}")
    return " · ".join(parts) if parts else None


def format_price_context(move: Optional[PriceMove]) -> List[str]:
    if move is None:
        return []
    change_24h = move.change_24h_pct if move.change_24h_pct is not None else move.window_changes.get(1440)
    head = f"TON: {fmt_price_move(move)}"
    if change_24h is not None:
        head += f" · {fmt_pct(change_24h)} за 24ч"
    lines = [head]
    windows = _window_line(move)
    if windows:
        lines.append(windows)
    if move.volume_24h_usd:
        volume = f"Объём 24ч: {fmt_usd(move.volume_24h_usd)}"
        if move.volume_ratio_vs_yesterday is not None:
            volume += f" ({fmt_ratio(move.volume_ratio_vs_yesterday)} к вчера)"
        lines.append(volume)
    if move.volume_1h_usd is not None:
        hourly = f"Объём за час: {fmt_usd(move.volume_1h_usd)}"
        if move.volume_1h_ratio is not None:
            hourly += f" — {fmt_times(move.volume_1h_ratio)} выше обычного" if move.volume_1h_ratio >= 1.5 else (
                f" ({fmt_ratio(move.volume_1h_ratio)} к обычному)"
            )
        lines.append(hourly)
    return lines


def format_causes(causes: Sequence[Any], limit: int = 2) -> List[str]:
    """Recent relevant headlines that may explain a market move.

    ``causes`` are ``storage.SeenItem``-like objects (title, url, source,
    published_at). Single-source items are labelled as unconfirmed.
    """
    if not causes:
        return ["Новостей, объясняющих движение, пока не найдено — проверьте источники."]
    lines = ["Возможная причина (не подтверждено):"]
    for cause in list(causes)[:limit]:
        title = html.escape(truncate(cause.title, 120))
        if getattr(cause, "url", None):
            title = f'<a href="{html.escape(cause.url, quote=True)}">{title}</a>'
        lines.append(f"• {title} — {html.escape(cause.source)}, {fmt_time(cause.published_at)}")
    return lines


def format_news_alert(
    item: NewsItem,
    classification: Classification,
    source_count: int,
    sources: Sequence[str] = (),
    verified: bool = True,
    price_move: Optional[PriceMove] = None,
    market_confirmed: bool = False,
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
    if market_confirmed:
        source_line += " · совпадает с движением рынка"
    elif not verified:
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


def format_price_alert(
    move: PriceMove,
    window_minutes: Optional[int] = None,
    causes: Sequence[Any] = (),
) -> str:
    """Alert for a price move over ``window_minutes`` (default: the fast window)."""
    minutes = window_minutes or move.window_minutes
    change = move.window_changes.get(minutes)
    if change is None:
        change = move.window_change_pct or 0.0
    emoji = "📈" if change > 0 else "📉"
    if minutes < 60:
        direction = "Резкий рост" if change > 0 else "Резкое падение"
    else:
        direction = "Рост" if change > 0 else "Падение"
    lines = [f"{emoji} {direction} TON: {fmt_pct(change)} за {fmt_window(minutes)}", ""]
    lines.extend(format_price_context(move))
    lines.append("")
    lines.extend(format_causes(causes))
    lines.append(DISCLAIMER)
    return truncate("\n".join(lines))


def format_impulse_alert(move: PriceMove, fast_minutes: int, causes: Sequence[Any] = ()) -> str:
    """Early warning: a fast move confirmed by the hourly direction, but not yet
    large enough for a regular price alert."""
    change = move.window_changes.get(fast_minutes)
    if change is None:
        change = move.window_change_pct or 0.0
    lines = [f"⚡ Импульс TON: {fmt_pct(change)} за {fmt_window(fast_minutes)}", ""]
    lines.extend(format_price_context(move))
    lines.append("")
    lines.extend(format_causes(causes))
    lines.append("")
    direction = "рост" if change > 0 else "падение"
    lines.append(
        "Раннее предупреждение: движение ещё не подтверждено, такие импульсы часто не продолжаются. "
        f"Если {direction} продолжится, придёт обычный сигнал."
    )
    lines.append(DISCLAIMER)
    return truncate("\n".join(lines))


def format_volume_alert(move: PriceMove, causes: Sequence[Any] = ()) -> str:
    """Alert for a trading-volume burst that is not (yet) a price alert."""
    ratio = move.volume_1h_ratio or 0.0
    change_1h = move.window_changes.get(60)
    head = f"📊 Всплеск объёма TON: {fmt_times(ratio)} выше обычного за час"
    if change_1h is not None:
        head += f" ({fmt_pct(change_1h)} за 1 ч)"
    lines = [head, ""]
    lines.extend(format_price_context(move))
    lines.append("")
    lines.extend(format_causes(causes))
    lines.append(DISCLAIMER)
    return truncate("\n".join(lines))


def format_startup(
    feed_count: int,
    keywords: Sequence[str],
    classifier_name: str,
    commands_enabled: bool,
    onchain: bool = False,
    restart_count: int = 0,
    last_exit_code: Optional[int] = None,
    whale_alert: bool = False,
    futures: bool = False,
) -> str:
    title = "🤖 GRAM/TON монитор запущен"
    if restart_count:
        title = f"♻️ GRAM/TON монитор перезапущен (№{restart_count}, код выхода {last_exit_code})"
    lines = [
        title,
        f"Лент: {feed_count} · ключевых слов: {len(keywords)}",
        f"Классификатор: {html.escape(classifier_name)}",
    ]
    if onchain:
        lines.append("Ончейн: крупные переводы и состояние сети — включено")
    elif whale_alert:
        lines.append("Крупные переводы: по данным Whale Alert")
    if futures:
        lines.append("Деривативы: финансирование, открытый интерес, ликвидации — включено")
    if commands_enabled:
        commands = ["/status", "/price"]
        if onchain or whale_alert:
            commands.append("/whales")
        if futures:
            commands.append("/futures")
        commands += ["/recent", "/stats", "/feeds", "/help"]
        lines.append(f"Команды: {' '.join(commands)}")
    return "\n".join(lines)


def fmt_ton(value: float) -> str:
    if value >= 1e9:
        return f"{value / 1e9:.2f} млрд TON".replace(".", ",")
    if value >= 1e6:
        return f"{value / 1e6:.2f} млн TON".replace(".", ",")
    if value >= 1e3:
        return f"{value / 1e3:.0f} тыс. TON"
    return f"{value:.0f} TON"


def _party(label_text: Optional[str], friendly: str) -> str:
    if not friendly:
        # Off-chain reports (Whale Alert) name the entity but give no address.
        return f"<b>{html.escape(label_text)}</b>" if label_text else "неизвестный кошелёк"
    short = f"{friendly[:6]}…{friendly[-4:]}" if len(friendly) > 12 else friendly
    link = f'<a href="https://tonviewer.com/{html.escape(friendly, quote=True)}">{html.escape(short)}</a>'
    if label_text:
        return f"<b>{html.escape(label_text)}</b> ({link})"
    return f"неизвестный кошелёк ({link})"


def format_whale_alert(transfer: Transfer, sentiment: str, strength: str, move: Optional[PriceMove] = None) -> str:
    header_emoji = {"negative": "🔴", "positive": "🟢"}.get(sentiment, "⚪")
    amount = fmt_ton(transfer.amount_ton)
    if transfer.usd_value:
        amount += f" (≈ {fmt_usd(transfer.usd_value)})"
    elif move is not None:
        amount += f" (≈ {fmt_usd(transfer.amount_ton * move.price_usd)})"
    lines = [
        f"🐋 {header_emoji} Крупный перевод: <b>{amount}</b>",
        "",
        f"Откуда: {_party(transfer.source_label.display() if transfer.source_label else None, transfer.source_friendly)}",
        f"Куда: {_party(transfer.destination_label.display() if transfer.destination_label else None, transfer.destination_friendly)}",
        f"Тип: {KIND_LABELS.get(transfer.kind, transfer.kind)}",
        f"Сила: {STRENGTH_LABELS.get(strength, strength)}",
    ]
    price_lines = format_price_context(move)
    if price_lines:
        lines.append("")
        lines.extend(price_lines)
    link_text = "Whale Alert" if transfer.link else "Транзакция"
    lines.extend(["", f'🔗 <a href="{html.escape(transfer.url, quote=True)}">{link_text}</a> · {fmt_time(transfer.utime)}', DISCLAIMER])
    return truncate("\n".join(lines))


def format_network_alert(age_seconds: float, seqno: int, recovered: bool = False) -> str:
    minutes = age_seconds / 60.0
    if recovered:
        return "\n".join([
            "🟢 Сеть TON снова производит блоки",
            f"Пауза длилась ≈ {minutes:.0f} мин · последний блок #{seqno}",
            DISCLAIMER,
        ])
    return "\n".join([
        "🔴 Возможная остановка сети TON",
        f"Последний блок мастерчейна #{seqno} был {minutes:.0f} мин назад.",
        "Обычно блоки идут каждые ~5 секунд; проверьте официальные каналы @tonstatus и биржи (возможны задержки ввода/вывода).",
        DISCLAIMER,
    ])


PROVIDER_TITLES = {"binance": "Binance Futures", "bybit": "Bybit", "okx": "OKX"}


def fmt_amount_usd(value: Optional[float]) -> str:
    """Like ``fmt_usd`` but for sums, where sub-dollar precision is noise."""
    if value is not None and 0 <= value < 1e3:
        return f"${value:,.0f}"
    return fmt_usd(value)


def fmt_coins(value: Optional[float], asset: str = "GRAM") -> str:
    if value is None:
        return "н/д"
    if value >= 1e9:
        return f"{value / 1e9:.2f} млрд {asset}".replace(".", ",")
    if value >= 1e6:
        return f"{value / 1e6:.1f} млн {asset}".replace(".", ",")
    if value >= 1e3:
        return f"{value / 1e3:.0f} тыс. {asset}"
    return f"{value:.0f} {asset}"


def _funding_line(snapshot: FuturesSnapshot) -> Optional[str]:
    if snapshot.funding_rate is None:
        return None
    interval = snapshot.funding_interval_hours
    interval_text = f"{interval:g} ч" if interval else "период"
    line = f"Ставка финансирования: {fmt_pct(snapshot.funding_pct, 3)} за {interval_text}"
    daily = snapshot.funding_daily_pct
    if daily is not None:
        line += f" (≈ {fmt_pct(daily, 2)}/день, {fmt_pct(snapshot.funding_annual_pct, 0)}/год)"
    if snapshot.next_funding_at:
        line += f" · следующая в {time.strftime('%H:%M', time.localtime(snapshot.next_funding_at))}"
    return line


def _oi_line(snapshot: FuturesSnapshot, asset: str) -> Optional[str]:
    if snapshot.open_interest is None and snapshot.open_interest_usd is None:
        return None
    if snapshot.open_interest is not None:
        text = fmt_coins(snapshot.open_interest, asset)
        if snapshot.open_interest_usd:
            text += f" (≈ {fmt_usd(snapshot.open_interest_usd)})"
    else:
        text = fmt_usd(snapshot.open_interest_usd)
    changes = " · ".join(
        f"{fmt_pct(change)} за {fmt_window(minutes)}" for minutes, change in sorted(snapshot.oi_changes.items())
    )
    line = f"Открытый интерес: {text}"
    if changes:
        line += f" · {changes}"
    return line


def _liquidation_line(stats: Optional[LiquidationStats]) -> Optional[str]:
    if stats is None:
        return None
    window = fmt_window(int(round(stats.window_seconds / 60)))
    if stats.total_usd <= 0:
        return f"Ликвидации за {window}: не зафиксированы ({PROVIDER_TITLES.get(stats.provider, stats.provider)})"
    prefix = "не менее " if stats.truncated else ""
    line = f"Ликвидации за {window}: {prefix}{fmt_amount_usd(stats.total_usd)} — лонги {fmt_amount_usd(stats.long_usd)}, шорты {fmt_amount_usd(stats.short_usd)}"
    line += f" ({PROVIDER_TITLES.get(stats.provider, stats.provider)}, {stats.count} ордеров)"
    return line


def _liquidation_note(stats: LiquidationStats) -> str:
    share = stats.long_share or 0.0
    if share >= 0.7:
        return "ликвидируют в основном лонги — принудительные продажи усиливают падение, пока каскад не выдохнется"
    if share <= 0.3:
        return "ликвидируют в основном шорты — принудительные покупки подталкивают цену вверх (шорт-сквиз)"
    return "ликвидации идут с обеих сторон — волатильность высокая, направление неясно"


def futures_body(
    snapshot: FuturesSnapshot,
    liquidations: Optional[LiquidationStats],
    asset: str,
    price_change_pct: Optional[float] = None,
    oi_hit: Optional[Tuple[int, float, float]] = None,
    funding_hit: bool = False,
    liquidation_hit: bool = False,
) -> List[str]:
    lines = [line for line in (_liquidation_line(liquidations), _oi_line(snapshot, asset), _funding_line(snapshot)) if line]
    notes = []
    if liquidation_hit and liquidations is not None:
        notes.append(_liquidation_note(liquidations))
    if oi_hit is not None:
        notes.append(positioning_note(oi_hit[1], price_change_pct))
    if funding_hit and snapshot.funding_daily_pct is not None:
        notes.append(funding_note(snapshot.funding_daily_pct))
    if notes:
        lines.append("Что это значит: " + "; ".join(notes) + ".")
    return lines


def format_futures_alert(
    snapshot: FuturesSnapshot,
    liquidations: Optional[LiquidationStats],
    asset: str = "GRAM",
    funding_hit: bool = False,
    oi_hit: Optional[Tuple[int, float, float]] = None,
    liquidation_hit: bool = False,
    move: Optional[PriceMove] = None,
    causes: Sequence[Any] = (),
) -> str:
    """Informational alert about derivatives positioning. The headline is the
    strongest trigger (liquidations > open interest > funding)."""
    if liquidation_hit and liquidations is not None:
        share = liquidations.long_share
        side = ""
        if share is not None:
            side = f" (лонги {share * 100:.0f}%)" if share >= 0.5 else f" (шорты {(1 - share) * 100:.0f}%)"
        prefix = "не менее " if liquidations.truncated else ""
        head = f"⚡ Деривативы TON: ликвидации {prefix}{fmt_amount_usd(liquidations.total_usd)} за {fmt_window(int(round(liquidations.window_seconds / 60)))}{side}"
    elif oi_hit is not None:
        minutes, change, _ = oi_hit
        head = f"📐 Деривативы TON: открытый интерес {fmt_pct(change)} за {fmt_window(minutes)}"
    else:
        head = f"💸 Деривативы TON: ставка финансирования {fmt_pct(snapshot.funding_daily_pct, 2)} в день"
    price_change = None
    if move is not None and oi_hit is not None:
        price_change = move.window_changes.get(oi_hit[0])
    lines = [head, ""]
    lines.extend(futures_body(snapshot, liquidations, asset, price_change, oi_hit, funding_hit, liquidation_hit))
    price_lines = format_price_context(move)
    if price_lines:
        lines.append("")
        lines.extend(price_lines)
    if liquidation_hit:
        lines.append("")
        lines.extend(format_causes(causes))
    lines.append("")
    lines.append(f"<i>Источник: {PROVIDER_TITLES.get(snapshot.provider, snapshot.provider)} · {fmt_time(snapshot.fetched_at)}</i>")
    lines.append(DISCLAIMER)
    return truncate("\n".join(lines))


def format_futures_status(
    snapshot: Optional[FuturesSnapshot],
    liquidations: Optional[LiquidationStats],
    asset: str = "GRAM",
    funding_threshold_daily_pct: float = 0.0,
    oi_windows: Sequence[Tuple[int, float]] = (),
    liquidation_threshold_usd: float = 0.0,
    history_seconds: float = 0.0,
    error: Optional[str] = None,
    move: Optional[PriceMove] = None,
) -> str:
    """``/futures`` reply: current derivatives context plus the alert thresholds."""
    lines = ["📐 <b>Деривативы TON (бессрочные фьючерсы)</b>"]
    if snapshot is None:
        lines.append("Данные пока недоступны" + (f": {html.escape(error[:80])}" if error else "."))
    else:
        oi_hit = max(snapshot.oi_changes.items(), key=lambda kv: abs(kv[1])) if snapshot.oi_changes else None
        price_change = move.window_changes.get(oi_hit[0]) if (move is not None and oi_hit) else None
        lines.extend(
            futures_body(
                snapshot, liquidations, asset, price_change,
                oi_hit=(oi_hit[0], oi_hit[1], 0.0) if oi_hit and abs(oi_hit[1]) >= 1.0 else None,
            )
        )
        lines.append(f"<i>Источник: {PROVIDER_TITLES.get(snapshot.provider, snapshot.provider)} · {fmt_time(snapshot.fetched_at)}</i>")
    thresholds = []
    if funding_threshold_daily_pct > 0:
        thresholds.append(f"финансирование ≥ {fmt_pct(funding_threshold_daily_pct, 2).lstrip('+')}/день")
    if oi_windows:
        thresholds.append("OI " + ", ".join(f"±{fmt_pct(t, 0).lstrip('+')}/{fmt_window(m)}" for m, t in oi_windows))
    if liquidation_threshold_usd > 0:
        thresholds.append(f"ликвидации ≥ {fmt_usd(liquidation_threshold_usd)}/час")
    if thresholds:
        lines.append("Алерты: " + " · ".join(thresholds))
    if snapshot is not None and oi_windows:
        longest_hours = max(m for m, _ in oi_windows) / 60
        history_hours = history_seconds / 3600
        if history_hours < longest_hours:
            covered = f"{history_hours:.1f}".replace(".", ",") if history_hours < 10 else f"{history_hours:.0f}"
            lines.append(f"<i>История открытого интереса: {covered} ч из {longest_hours:.0f} ч — длинные окна заработают позже.</i>")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


class TelegramNotifier:
    """Minimal Telegram Bot API client (HTML parse mode)."""

    def __init__(self, token: str, chat_id: str, timeout: float = 15.0):
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout
        self._session = requests.Session()
        self.last_error: Optional[str] = None
        self.last_error_code: Optional[int] = None

    @property
    def is_configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def _call(self, method: str, request_timeout: Optional[float] = None, **params: Any) -> Optional[Dict[str, Any]]:
        if not self.token:
            return None
        url = TELEGRAM_API.format(token=self.token, method=method)
        self.last_error = None
        self.last_error_code = None
        for attempt in range(2):
            try:
                response = self._session.post(url, json=params, timeout=request_timeout or self.timeout)
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                self.last_error = str(exc)
                logger.warning("Telegram %s failed: %s", method, exc)
                return None
            if payload.get("ok"):
                return payload
            self.last_error = payload.get("description")
            self.last_error_code = payload.get("error_code")
            retry_after = (payload.get("parameters") or {}).get("retry_after")
            if self.last_error_code == 429 and attempt == 0 and retry_after is not None:
                wait = min(float(retry_after), MAX_FLOOD_WAIT_SECONDS)
                logger.info("Telegram flood control on %s; waiting %.0fs", method, wait)
                time.sleep(wait)
                continue
            logger.warning("Telegram %s error: %s", method, self.last_error)
            return None
        return None

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
        if payload is None and self.last_error_code == 400 and "parse" in (self.last_error or "").lower():
            # Telegram rejected the HTML markup: resend as plain text.
            payload = self._call("sendMessage", chat_id=chat_id, text=html.unescape(_TAG_RE.sub("", text)))
        return payload is not None

    def get_me(self) -> Optional[str]:
        """Bot username, or None when the token is rejected / API unreachable."""
        payload = self._call("getMe")
        if payload is None:
            return None
        return (payload.get("result") or {}).get("username")

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
