"""Self-monitoring: component health, heartbeat, memory and the watchdog.

The bot is meant to run unattended. Three layers keep it alive:

* :class:`HealthTracker` turns per-poll ok/fail reports into a single
  Telegram notice when a component (feeds, price, on-chain, telegram) has been
  failing continuously for a while, and a recovery notice afterwards.
* :class:`Watchdog` runs in a daemon thread and terminates the process when the
  main loop stops beating or the process grows past ``max_memory_mb``; the
  supervisor (``grambot.supervisor``, Docker, systemd, launchd) restarts it.
* A heartbeat file lets external health checks (``python main.py
  --healthcheck``, Docker ``HEALTHCHECK``) see that the loop is alive.
"""
from __future__ import annotations

import logging
import os
import resource
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

EXIT_WATCHDOG = 75  # main loop hung (EX_TEMPFAIL): supervisor should restart
EXIT_MEMORY = 76  # memory limit exceeded: supervisor should restart

COMPONENT_TITLES = {
    "feeds": "источники новостей",
    "price": "провайдеры цены",
    "onchain": "TON Center (ончейн)",
    "telegram": "Telegram API",
}


def current_rss_mb() -> Optional[float]:
    """Resident memory of this process in MiB (Linux: live; macOS: high-water mark)."""
    try:
        with open("/proc/self/statm") as fh:
            pages = int(fh.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        pass
    try:
        maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (ValueError, OSError):  # pragma: no cover - platform without getrusage
        return None
    # ru_maxrss is bytes on macOS and kilobytes on Linux.
    return maxrss / (1024 * 1024) if sys.platform == "darwin" else maxrss / 1024


def write_heartbeat(path: str, payload: str = "") -> None:
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload or f"{time.time():.0f}\n")
        os.replace(tmp, path)
    except OSError as exc:
        logger.debug("Heartbeat write failed: %s", exc)


def heartbeat_age(path: str) -> Optional[float]:
    try:
        return max(0.0, time.time() - os.stat(path).st_mtime)
    except OSError:
        return None


@dataclass
class ComponentState:
    failing_since: Optional[float] = None
    last_error: Optional[str] = None
    alerted: bool = False
    last_ok_at: Optional[float] = None
    failures: int = 0


@dataclass
class HealthEvent:
    kind: str  # "down" | "up"
    component: str
    duration_seconds: float
    detail: Optional[str] = None


@dataclass
class HealthTracker:
    """Collapses noisy per-poll failures into one down/up notice per outage."""

    alert_after_seconds: float = 3600.0
    states: Dict[str, ComponentState] = field(default_factory=dict)

    def state(self, component: str) -> ComponentState:
        return self.states.setdefault(component, ComponentState())

    def report(self, component: str, ok: bool, detail: Optional[str] = None, now: Optional[float] = None) -> Optional[HealthEvent]:
        now = now or time.time()
        state = self.state(component)
        if ok:
            event = None
            if state.alerted and state.failing_since is not None:
                event = HealthEvent("up", component, now - state.failing_since)
            state.failing_since = None
            state.alerted = False
            state.failures = 0
            state.last_error = None
            state.last_ok_at = now
            return event
        state.failures += 1
        state.last_error = detail
        if state.failing_since is None:
            state.failing_since = now
            return None
        if not state.alerted and now - state.failing_since >= self.alert_after_seconds:
            state.alerted = True
            return HealthEvent("down", component, now - state.failing_since, detail)
        return None

    def summary(self, now: Optional[float] = None) -> List[Tuple[str, str]]:
        """(component, human status) pairs for /status."""
        now = now or time.time()
        rows = []
        for component, state in self.states.items():
            if state.failing_since is None:
                rows.append((component, "ок"))
            else:
                minutes = (now - state.failing_since) / 60
                rows.append((component, f"сбой {minutes:.0f} мин ({state.failures}×)"))
        return rows


def format_health_event(event: HealthEvent) -> str:
    title = COMPONENT_TITLES.get(event.component, event.component)
    minutes = event.duration_seconds / 60
    if event.kind == "up":
        return f"🟢 Восстановлено: {title} снова отвечают (сбой длился ≈ {minutes:.0f} мин)."
    detail = f"\nПоследняя ошибка: {event.detail[:120]}" if event.detail else ""
    return (
        f"⚠️ Техническое: {title} не отвечают уже {minutes:.0f} мин.{detail}\n"
        "Бот продолжает работать и повторяет попытки сам; сигналы из этого источника пока не приходят."
    )


class Watchdog(threading.Thread):
    """Kill the process if the main loop hangs or memory runs away."""

    def __init__(
        self,
        heartbeat: Callable[[], float],
        stop: Callable[[int, str], None],
        hang_timeout: float,
        max_memory_mb: float,
        check_interval: float = 30.0,
        grace_seconds: float = 30.0,
        exit_fn: Callable[[int], None] = os._exit,
    ):
        super().__init__(name="watchdog", daemon=True)
        self.heartbeat = heartbeat
        self.stop_monitor = stop
        self.hang_timeout = hang_timeout
        self.max_memory_mb = max_memory_mb
        self.check_interval = check_interval
        self.grace_seconds = grace_seconds
        self.exit_fn = exit_fn
        self.stop_event = threading.Event()
        self.triggered: Optional[str] = None

    def check(self) -> Optional[Tuple[int, str]]:
        age = time.monotonic() - self.heartbeat()
        if self.hang_timeout and age > self.hang_timeout:
            return EXIT_WATCHDOG, f"главный цикл не отвечает {age / 60:.0f} мин"
        rss = current_rss_mb()
        if self.max_memory_mb and rss is not None and rss > self.max_memory_mb:
            return EXIT_MEMORY, f"память {rss:.0f} МБ превысила лимит {self.max_memory_mb:.0f} МБ"
        return None

    def run(self) -> None:
        while not self.stop_event.wait(self.check_interval):
            problem = self.check()
            if problem is None:
                continue
            code, reason = problem
            self.triggered = reason
            logger.critical("Watchdog: %s — restarting (exit %d)", reason, code)
            try:
                self.stop_monitor(code, reason)
            except Exception:  # pragma: no cover - best effort
                logger.exception("Graceful stop failed")
            # Give the main thread a chance to close the database; then force.
            self.stop_event.wait(self.grace_seconds)
            logging.shutdown()
            self.exit_fn(code)
            return

    def cancel(self) -> None:
        self.stop_event.set()
