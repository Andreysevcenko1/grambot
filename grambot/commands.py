"""Telegram command handling (long polling in a background thread).

Only the configured ``TELEGRAM_CHAT_ID`` may talk to the bot; everything else
is ignored. Commands are intentionally read-mostly: they inspect state and
toggle muting, they never change sources or thresholds at runtime.
"""
from __future__ import annotations

import html
import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from . import price as price_module
from .notifier import (
    DISCLAIMER,
    TelegramNotifier,
    fmt_pct,
    fmt_time,
    fmt_ton,
    fmt_usd,
    format_news_alert,
    format_price_context,
)
from .health import COMPONENT_TITLES, current_rss_mb
from .onchain import KIND_LABELS
from .processing.classifier import Classification
from .sources import NewsItem

if TYPE_CHECKING:  # pragma: no cover
    from .app import GramTonMonitor

logger = logging.getLogger(__name__)

OFFSET_KEY = "tg_update_offset"
MUTED_UNTIL_KEY = "muted_until"

COMMANDS = [
    ("status", "Состояние бота"),
    ("price", "Цена TON и движение"),
    ("whales", "Крупные ончейн-переводы за 24ч"),
    ("recent", "Последние релевантные новости"),
    ("stats", "Статистика сигналов"),
    ("feeds", "Состояние источников"),
    ("check", "Проверить ленты сейчас"),
    ("test", "Отправить тестовый сигнал"),
    ("mute", "Отключить уведомления на N минут"),
    ("unmute", "Включить уведомления"),
    ("help", "Справка"),
]

HELP_TEXT = "\n".join(
    [
        "🤖 <b>GRAM/TON монитор</b>",
        "Бот собирает новости о TON/GRAM/Telegram, объединяет одинаковые, "
        "оценивает вероятное влияние и присылает <i>информационные</i> сигналы.",
        "",
        "/status — состояние бота",
        "/price — цена TON, изменение и объём",
        "/whales [N] — крупные переводы и потоки бирж за 24ч",
        "/recent [N] — последние релевантные новости",
        "/stats — как сигналы соотносились с ценой",
        "/feeds — какие источники работают",
        "/check — проверить ленты прямо сейчас",
        "/test — тестовый сигнал (проверка формата)",
        "/mute [минуты] — пауза уведомлений (по умолчанию 60)",
        "/unmute — снять паузу",
        "",
        DISCLAIMER,
    ]
)


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}д {hours}ч"
    if hours:
        return f"{hours}ч {minutes}м"
    return f"{minutes}м"


class CommandHandler(threading.Thread):
    def __init__(self, monitor: "GramTonMonitor", notifier: TelegramNotifier):
        super().__init__(name="telegram-commands", daemon=True)
        self.monitor = monitor
        self.notifier = notifier
        self.storage = monitor.storage
        self.settings = monitor.settings
        self.stop_event = monitor.stop_event
        self.allowed_chat_id = str(self.settings.telegram_chat_id)

    # -- polling loop ---------------------------------------------------
    def _initial_offset(self) -> Optional[int]:
        stored = self.storage.get_value(OFFSET_KEY)
        if stored is not None:
            try:
                return int(stored)
            except ValueError:
                pass
        # First start: skip whatever accumulated while the bot was offline so
        # old "/start" messages are not replayed.
        latest = self.notifier.get_updates(offset=-1, timeout_seconds=0) or []
        if latest:
            return latest[-1]["update_id"] + 1
        return None

    def run(self) -> None:
        if not self.notifier.is_configured:
            logger.info("Telegram not configured; command handler idle")
            return
        self.notifier.set_commands(COMMANDS)
        offset: Optional[int] = None
        try:
            offset = self._initial_offset()
        except Exception:  # pragma: no cover - storage hiccup; start from "now"
            logger.exception("Could not read the saved update offset")
        logger.info("Command handler started (offset=%s)", offset)
        while not self.stop_event.is_set():
            try:
                offset = self._poll_once(offset)
            except Exception:  # pragma: no cover - never let anything kill the thread
                logger.exception("Command handler iteration failed")
                self.stop_event.wait(5)

    def _poll_once(self, offset: Optional[int]) -> Optional[int]:
        updates = self.notifier.get_updates(offset, timeout_seconds=20)
        if updates is None:
            # Network error or another bot instance polling (409): back off.
            self.stop_event.wait(10)
            return offset
        if not updates:
            self.stop_event.wait(1)
            return offset
        for update in updates:
            offset = update["update_id"] + 1
            try:
                self.handle_update(update)
            except Exception:  # pragma: no cover - never let one command kill the loop
                logger.exception("Failed to handle update %s", update.get("update_id"))
        self.storage.set_value(OFFSET_KEY, str(offset))
        return offset

    # -- dispatch -------------------------------------------------------
    def handle_update(self, update: Dict[str, Any]) -> None:
        message = update.get("message") or update.get("edited_message")
        if not message:
            return
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return
        if chat_id != self.allowed_chat_id:
            logger.info("Ignoring command from unauthorized chat %s", chat_id)
            return
        logger.info("Command %s", text.split()[0])
        reply = self.handle_command(text)
        if reply:
            self.notifier.send_to(chat_id, reply)

    def handle_command(self, text: str) -> str:
        parts = text.split()
        command = parts[0].split("@")[0].lower()
        args = parts[1:]
        handlers = {
            "/start": self.cmd_help,
            "/help": self.cmd_help,
            "/status": self.cmd_status,
            "/price": self.cmd_price,
            "/whales": self.cmd_whales,
            "/recent": self.cmd_recent,
            "/stats": self.cmd_stats,
            "/feeds": self.cmd_feeds,
            "/check": self.cmd_check,
            "/test": self.cmd_test,
            "/mute": self.cmd_mute,
            "/unmute": self.cmd_unmute,
        }
        handler = handlers.get(command)
        if handler is None:
            return "Неизвестная команда. /help — список команд."
        return handler(args)

    # -- commands ----------------------------------------------------------
    def cmd_help(self, args: List[str]) -> str:
        return HELP_TEXT

    def cmd_status(self, args: List[str]) -> str:
        m = self.monitor
        now = time.time()
        day_ago = now - 86400
        results = m.last_feed_results
        ok = sum(1 for r in results if r.ok)
        lines = ["📊 <b>Состояние</b>", f"Работает: {_fmt_duration(now - m.started_at)}"]
        if m.last_news_poll_at:
            lines.append(f"Последняя проверка лент: {fmt_time(m.last_news_poll_at)}")
        lines.append(f"Источники: {ok}/{len(results)} ок" if results else f"Источники: {len(self.settings.rss_feeds)} (ещё не опрошены)")
        lines.append(f"Релевантных новостей за 24ч: {self.storage.count_items_since(day_ago)}")
        lines.append(f"Сигналов за 24ч: {self.storage.count_signals_since(day_ago)}")
        lines.append(f"Классификатор: {html.escape(m.classifier_name)}")
        lines.append(f"Интервал: новости {self.settings.poll_interval_seconds // 60} мин, цена {self.settings.price_poll_interval_seconds // 60} мин")
        lines.append(self._onchain_status_line())
        lines.append(self._health_status_line())
        muted_until = m.muted_until()
        if muted_until:
            lines.append(f"🔇 Уведомления на паузе до {fmt_time(muted_until)}")
        return "\n".join(lines)

    def _health_status_line(self) -> str:
        m = self.monitor
        parts = []
        rss = current_rss_mb()
        if rss is not None:
            limit = f"/{self.settings.max_memory_mb:.0f}" if self.settings.max_memory_mb else ""
            parts.append(f"память {rss:.0f}{limit} МБ")
        parts.append(f"база {self.storage.size_bytes() / (1024 * 1024):.1f} МБ")
        if m.restart_count:
            parts.append(f"перезапусков подряд: {m.restart_count}")
        failing = [f"{COMPONENT_TITLES.get(c, c)}: {status}" for c, status in m.health.summary() if status != "ок"]
        line = "Здоровье: " + " · ".join(parts)
        if failing:
            line += "\n⚠️ " + "; ".join(failing)
        return line

    def cmd_price(self, args: List[str]) -> str:
        move = self.monitor.poll_price(alert=False)
        if move is None:
            move = self.monitor._localize(
                price_module.move_from_history(self.storage, self.settings.price_window_minutes)
            )
        if move is None:
            return "Данные о цене пока недоступны."
        lines = ["💰 <b>TON / GRAM</b>"] + format_price_context(move)
        provider = self.monitor.price_client.last_provider or move.provider
        lines.append(f"<i>Источник: {html.escape(provider)} · {fmt_time(time.time())}</i>")
        return "\n".join(lines)

    def _onchain_status_line(self) -> str:
        info = self.monitor.onchain_status()
        if not info["enabled"]:
            return "Ончейн: выключен"
        if info["backoff_seconds"] > 0:
            return f"Ончейн: лимит TON Center, пауза {info['backoff_seconds'] / 60:.0f} мин"
        if info["error"]:
            return f"Ончейн: ошибка — {html.escape(str(info['error'])[:60])}"
        if info["last_poll_at"] is None:
            return f"Ончейн: ожидает первого опроса · меток адресов: {info['labels']}"
        parts = [f"Ончейн: отставание {_fmt_duration(info['lag_seconds'] or 0)}"]
        if info["block_age_seconds"] is not None:
            parts.append(f"последний блок #{info['seqno']} {info['block_age_seconds']:.0f}с назад")
        parts.append(f"меток адресов: {info['labels']}")
        return " · ".join(parts)

    def cmd_whales(self, args: List[str]) -> str:
        if not self.monitor.onchain_enabled:
            return "Ончейн-мониторинг выключен (ENABLE_ONCHAIN=false)."
        limit = 5
        if args and args[0].isdigit():
            limit = max(1, min(15, int(args[0])))
        day_ago = time.time() - 86400
        flows = self.storage.flow_stats(day_ago)
        transfers = self.storage.recent_transfers(day_ago, limit=limit)
        move = self.monitor.current_price_move()
        price = move.price_usd if move else None

        def usd(amount_ton: float) -> str:
            return f" (≈ {fmt_usd(amount_ton * price)})" if price else ""

        threshold = self.settings.whale_min_ton / 10.0
        lines = [
            "🐋 <b>Ончейн за 24ч</b>",
            f"Переводов ≥ {fmt_ton(threshold)}: {flows.total_count} на {fmt_ton(flows.total_ton)}{usd(flows.total_ton)}",
            f"На биржи: {fmt_ton(flows.deposits_ton)} ({flows.deposits_count}) · с бирж: {fmt_ton(flows.withdrawals_ton)} ({flows.withdrawals_count})",
        ]
        if flows.deposits_count or flows.withdrawals_count:
            net = flows.net_ton
            verdict = "отток с бирж (чаще накопление)" if net > 0 else "приток на биржи (возможное давление продаж)" if net < 0 else "баланс"
            lines.append(f"Нетто: {'+' if net > 0 else ''}{fmt_ton(abs(net)) if net else '0 TON'} — {verdict}")
        if transfers:
            lines.append("")
            lines.append(f"Крупнейшие {len(transfers)}:")
            for t in transfers:
                src = html.escape(t.source_label or "неизвестный")
                dst = html.escape(t.destination_label or "неизвестный")
                mark = " 🔔" if t.notified else ""
                lines.append(
                    f'• <a href="https://tonviewer.com/transaction/{html.escape(t.hash, quote=True)}">{fmt_ton(t.amount_ton)}</a>'
                    f" {src} → {dst}\n  <i>{KIND_LABELS.get(t.kind, t.kind)} · {fmt_time(t.utime)}</i>{mark}"
                )
        else:
            lines.append("")
            lines.append("Крупных переводов за сутки пока не зафиксировано.")
        info = self.monitor.onchain_status()
        if info["lag_seconds"] is not None:
            lines.append(f"<i>Данные TON Center, отставание {_fmt_duration(info['lag_seconds'])}; метки адресов: ton-labels.</i>")
        lines.append(DISCLAIMER)
        return "\n".join(lines)

    def cmd_recent(self, args: List[str]) -> str:
        limit = 5
        if args and args[0].isdigit():
            limit = max(1, min(15, int(args[0])))
        items = self.storage.recent_items(time.time() - 7 * 86400, limit=limit)
        if not items:
            return "Релевантных новостей за последние 7 дней пока нет."
        lines = [f"🗞 <b>Последние {len(items)} новостей</b>"]
        for it in items:
            title = html.escape(it.title)
            if it.url:
                title = f'<a href="{html.escape(it.url, quote=True)}">{title}</a>'
            lines.append(f"• {title}\n  <i>{html.escape(it.source)} · {fmt_time(it.published_at)}</i>")
        return "\n".join(lines)

    def cmd_stats(self, args: List[str]) -> str:
        s = self.storage.signal_stats()
        lines = [
            "📈 <b>Статистика сигналов</b>",
            f"Новостных сигналов: {s.total_news}",
            f"Ценовых алертов: {s.total_price}",
            f"Ончейн-сигналов: {s.total_onchain}",
        ]

        def block(label: str, evaluated: int, hits: int, avg: Optional[float]) -> str:
            if not evaluated:
                return f"{label}: пока нет оценённых сигналов"
            rate = hits / evaluated * 100
            avg_txt = fmt_pct(avg, 2).lstrip("+") if avg is not None else "н/д"
            return f"{label}: направление совпало {hits}/{evaluated} ({rate:.0f}%), средний ход {avg_txt}"

        lines.append(block("Через 1ч", s.evaluated_1h, s.hits_1h, s.avg_abs_move_1h))
        lines.append(block("Через 24ч", s.evaluated_24h, s.hits_24h, s.avg_abs_move_24h))
        recent = self.storage.recent_signals(limit=3)
        if recent:
            lines.append("")
            lines.append("Последние:")
            for sig in recent:
                move = ""
                if sig.price_at_send and sig.price_after_1h:
                    move = f" → {fmt_pct((sig.price_after_1h - sig.price_at_send) / sig.price_at_send * 100)} за 1ч"
                lines.append(f"• {fmt_time(sig.sent_at)} {html.escape(sig.title[:70])}{move}")
        lines.append("")
        lines.append("Совпадение направления ≠ причинно-следственная связь.")
        return "\n".join(lines)

    def cmd_feeds(self, args: List[str]) -> str:
        results = self.monitor.last_feed_results
        if not results:
            return "Ленты ещё не опрашивались. Отправьте /check."
        lines = ["📡 <b>Источники</b>"]
        for r in results:
            name = html.escape(r.feed_title or r.url.replace("https://", "")[:60])
            if r.ok:
                lines.append(f"✅ {name} — {len(r.items)} записей, {r.duration_seconds:.1f}с")
            else:
                lines.append(f"❌ {name} — {html.escape((r.error or 'ошибка')[:80])}")
        return "\n".join(lines)

    def cmd_check(self, args: List[str]) -> str:
        self.monitor.request_poll()
        return "🔄 Проверяю ленты… результат появится в логе, сигналы — здесь."

    def cmd_test(self, args: List[str]) -> str:
        item = NewsItem(
            source="Тест",
            title="ТЕСТ: TON Foundation сообщила о сбое в сети",
            summary="Это тестовое сообщение для проверки формата уведомлений.",
            url="https://ton.org",
            published_at=time.time(),
        )
        classification = Classification(
            sentiment="negative", strength="high", matched_keywords=["сбой"], reason="сбой, TON Foundation"
        )
        move = self.monitor.current_price_move()
        return format_news_alert(item, classification, source_count=3, sources=["Тест", "Cointelegraph", "Decrypt"], price_move=move)

    def cmd_mute(self, args: List[str]) -> str:
        minutes = 60
        if args:
            if args[0].lower() in {"off", "0", "выкл"}:
                return self.cmd_unmute([])
            if args[0].isdigit():
                minutes = max(1, min(24 * 60 * 7, int(args[0])))
        until = time.time() + minutes * 60
        self.storage.set_value(MUTED_UNTIL_KEY, str(until))
        return f"🔇 Уведомления отключены до {fmt_time(until)} ({minutes} мин). /unmute — включить."

    def cmd_unmute(self, args: List[str]) -> str:
        self.storage.set_value(MUTED_UNTIL_KEY, None)
        return "🔔 Уведомления включены."
