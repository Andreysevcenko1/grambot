import logging
import os
import threading
import time
from unittest.mock import patch

import pytest

from grambot import app as app_module
from grambot import supervisor as supervisor_module
from grambot.app import ONCHAIN_LAST_UTIME_KEY, configure_logging, healthcheck
from grambot.config import Settings
from grambot.health import (
    EXIT_MEMORY,
    EXIT_WATCHDOG,
    HealthTracker,
    Watchdog,
    current_rss_mb,
    format_health_event,
    heartbeat_age,
    write_heartbeat,
)
from grambot.notifier import TelegramNotifier
from grambot.onchain import ScanResult
from grambot.supervisor import Supervisor, backoff_seconds, child_command, restart_info

from .conftest import FakeNotifier


# -- health tracker ------------------------------------------------------------
def test_health_tracker_alerts_once_and_recovers():
    tracker = HealthTracker(alert_after_seconds=600)
    assert tracker.report("feeds", False, "timeout", now=1000) is None  # outage starts
    assert tracker.report("feeds", False, "timeout", now=1300) is None  # not long enough
    event = tracker.report("feeds", False, "dns", now=1700)
    assert event is not None and event.kind == "down" and event.detail == "dns"
    assert tracker.report("feeds", False, "dns", now=2000) is None  # already alerted
    up = tracker.report("feeds", True, now=2500)
    assert up is not None and up.kind == "up" and up.duration_seconds == 1500
    assert tracker.report("feeds", True, now=2600) is None
    assert tracker.report("price", True, now=2600) is None
    assert tracker.report("price", False, now=2700) is None
    assert tracker.report("price", True, now=2800) is None  # short blip: no messages
    assert dict(tracker.summary(now=2800)) == {"feeds": "ок", "price": "ок"}
    tracker.report("onchain", False, "500", now=3000)
    tracker.report("onchain", False, "500", now=3120)
    assert dict(tracker.summary(now=3120))["onchain"] == "сбой 2 мин (2×)"

    down_text = format_health_event(event)
    assert "источники новостей" in down_text and "dns" in down_text and "не отвечают уже 12 мин" in down_text
    assert "Восстановлено" in format_health_event(up) and "25 мин" in format_health_event(up)


# -- heartbeat / memory ---------------------------------------------------------
def test_heartbeat_file_and_healthcheck(tmp_path):
    path = str(tmp_path / "hb")
    assert heartbeat_age(path) is None
    write_heartbeat(path)
    assert heartbeat_age(path) < 5
    assert not os.path.exists(path + ".tmp")
    settings = Settings(heartbeat_path=path)
    assert healthcheck(settings, max_age_seconds=60) == 0
    os.utime(path, (time.time() - 3600, time.time() - 3600))
    assert healthcheck(settings, max_age_seconds=60) == 1
    assert healthcheck(Settings(heartbeat_path=str(tmp_path / "missing")), 60) == 1
    assert Settings(database_path="/data/g.db").heartbeat_file == "/data/g.db.heartbeat"


def test_current_rss_is_reasonable():
    rss = current_rss_mb()
    assert rss is not None and 5 < rss < 4096


# -- watchdog --------------------------------------------------------------------
def test_watchdog_check_detects_hang_and_memory():
    beat = time.monotonic() - 1000
    stops = []
    dog = Watchdog(heartbeat=lambda: beat, stop=lambda c, r: stops.append((c, r)), hang_timeout=600, max_memory_mb=0)
    code, reason = dog.check()
    assert code == EXIT_WATCHDOG and "не отвечает" in reason

    dog = Watchdog(heartbeat=time.monotonic, stop=lambda c, r: None, hang_timeout=600, max_memory_mb=1)
    code, reason = dog.check()
    assert code == EXIT_MEMORY and "лимит 1 МБ" in reason

    dog = Watchdog(heartbeat=time.monotonic, stop=lambda c, r: None, hang_timeout=0, max_memory_mb=0)
    assert dog.check() is None


def test_watchdog_thread_stops_monitor_then_exits():
    stops = []
    exits = []
    dog = Watchdog(
        heartbeat=lambda: time.monotonic() - 100,
        stop=lambda c, r: stops.append(c),
        hang_timeout=10,
        max_memory_mb=0,
        check_interval=0.01,
        grace_seconds=0.01,
        exit_fn=exits.append,
    )
    dog.start()
    dog.join(timeout=5)
    assert stops == [EXIT_WATCHDOG] and exits == [EXIT_WATCHDOG]
    assert dog.triggered and not dog.is_alive()


# -- supervisor ------------------------------------------------------------------
def test_backoff_and_child_command():
    assert [backoff_seconds(n) for n in (0, 1, 2, 3, 6, 50)] == [5, 5, 15, 30, 300, 300]
    cmd = child_command(["--no-telegram", "--no-supervise"])
    assert cmd[0] and cmd[2] == "--no-supervise" and cmd[3:] == ["--no-telegram"]
    assert cmd.count("--no-supervise") == 1


class FakeChild:
    def __init__(self, code, uptime=0.0):
        self.code = code
        self.uptime = uptime
        self.signals = []

    def wait(self, timeout=None):
        return self.code

    def poll(self):
        return self.code

    def send_signal(self, sig):
        self.signals.append(sig)


def test_supervisor_restarts_until_clean_exit():
    exits = iter([1, 75, 0])
    spawned = []
    sleeps = []

    def popen(cmd, env=None):
        spawned.append((cmd, env))
        return FakeChild(next(exits))

    sup = Supervisor(["--no-telegram"], env={"X": "1"}, sleep=sleeps.append, popen=popen)
    sup.install_signal_handlers = lambda: None
    assert sup.run() == 0
    assert len(spawned) == 3 and sup.restarts == 2
    assert sleeps == [5, 15]
    counts = [env["GRAMBOT_RESTART_COUNT"] for _, env in spawned]
    codes = [env["GRAMBOT_LAST_EXIT_CODE"] for _, env in spawned]
    assert counts == ["0", "1", "2"] and codes == ["", "1", "75"]
    assert all(env["X"] == "1" for _, env in spawned)


def test_supervisor_resets_failures_after_stable_run():
    exits = iter([1, 1, 0])
    sleeps = []
    clock = {"t": 0.0}

    def monotonic():
        clock["t"] += supervisor_module.STABLE_RUN_SECONDS + 1  # every child "ran" long enough
        return clock["t"]

    sup = Supervisor([], env={}, sleep=sleeps.append, popen=lambda cmd, env=None: FakeChild(next(exits)))
    sup.install_signal_handlers = lambda: None
    with patch.object(supervisor_module.time, "monotonic", monotonic):
        assert sup.run() == 0
    assert sleeps == [5, 5]


def test_restart_info_parsing(monkeypatch):
    monkeypatch.setenv("GRAMBOT_RESTART_COUNT", "3")
    monkeypatch.setenv("GRAMBOT_LAST_EXIT_CODE", "75")
    assert restart_info() == (3, 75)
    monkeypatch.setenv("GRAMBOT_RESTART_COUNT", "x")
    monkeypatch.setenv("GRAMBOT_LAST_EXIT_CODE", "")
    assert restart_info() == (0, None)


# -- monitor integration -----------------------------------------------------------
def test_run_forever_beats_announces_and_returns_exit_code(monitor, tmp_path):
    monitor.settings.heartbeat_path = str(tmp_path / "hb")
    monitor.settings.enable_commands = False
    monitor.settings.watchdog_timeout_minutes = 0
    monitor.settings.max_memory_mb = 0
    monitor.onchain_enabled = False
    monitor.restart_count, monitor.last_exit_code = 2, 75

    result = {}

    def target():
        result["code"] = monitor.run_forever()

    thread = threading.Thread(target=target)
    thread.start()
    deadline = time.time() + 5
    while not os.path.exists(monitor.settings.heartbeat_file) and time.time() < deadline:
        time.sleep(0.05)
    monitor.request_restart(EXIT_MEMORY, "test")
    thread.join(timeout=5)
    assert result["code"] == EXIT_MEMORY
    assert heartbeat_age(monitor.settings.heartbeat_file) < 10
    assert monitor.notifier.sent and "перезапущен (№2, код выхода 75)" in monitor.notifier.sent[0]


def test_startup_message_suppressed_after_many_restarts(monitor):
    monitor.restart_count = 3
    monitor._announce_start()
    assert monitor.notifier.sent == []
    monitor.restart_count = 0
    monitor._announce_start()
    assert "запущен" in monitor.notifier.sent[0]


def test_ensure_command_handler_restarts_dead_thread(monitor):
    class DeadHandler:
        def is_alive(self):
            return False

    monitor.command_handler = DeadHandler()
    with patch.object(app_module.CommandHandler, "start", lambda self: None):
        monitor._ensure_command_handler()
        assert isinstance(monitor.command_handler, app_module.CommandHandler)
        first = monitor.command_handler
        first.is_alive = lambda: True
        monitor._ensure_command_handler()
        assert monitor.command_handler is first
    monitor.settings.enable_commands = False
    monitor.command_handler = None
    monitor._ensure_command_handler()
    assert monitor.command_handler is None


def test_persistent_failures_produce_one_notice_and_recovery(monitor):
    monitor.health.alert_after_seconds = 0.0
    monitor.notifier.sent.clear()
    monitor._report_health("price", False, "boom")  # outage starts, no message yet
    assert monitor.notifier.sent == []
    monitor._report_health("price", False, "boom")
    assert len(monitor.notifier.sent) == 1 and "провайдеры цены" in monitor.notifier.sent[0] and "boom" in monitor.notifier.sent[0]
    monitor._report_health("price", False, "boom")
    assert len(monitor.notifier.sent) == 1
    monitor._report_health("price", True)
    assert len(monitor.notifier.sent) == 2 and "Восстановлено" in monitor.notifier.sent[1]
    # Health notices are operational: never recorded as signals.
    assert monitor.storage.count_signals_since(0) == 0


def test_feed_and_price_polls_report_health(monitor):
    monitor.health.alert_after_seconds = 0.0
    monitor.poll_price()  # PriceClient.fetch is patched to None in the fixture
    monitor.poll_price()
    assert any("провайдеры цены" in t for t in monitor.notifier.sent)
    monitor.source.queue([])
    monitor.poll_news_once()
    assert monitor.health.state("feeds").failing_since is None


def test_onchain_stuck_window_is_skipped_after_repeated_errors(monitor):
    since = 1000.0
    monitor.storage.set_value(ONCHAIN_LAST_UTIME_KEY, str(since))
    failed = ScanResult(last_utime=since, complete=False, error="500 Server Error")
    for _ in range(app_module.ONCHAIN_STUCK_FAILURES - 1):
        monitor._track_onchain_progress(failed, since)
    assert monitor.storage.get_float(ONCHAIN_LAST_UTIME_KEY) == since
    monitor._track_onchain_progress(failed, since)
    assert monitor.storage.get_float(ONCHAIN_LAST_UTIME_KEY) == since + app_module.ONCHAIN_STUCK_SKIP_SECONDS
    assert monitor._onchain_failures == 0

    limited = ScanResult(last_utime=since, complete=False, error="rate limited", rate_limited=True)
    for _ in range(10):
        monitor._track_onchain_progress(limited, since)
    assert monitor._onchain_failures == 0  # rate limits never count as "stuck"
    monitor._track_onchain_progress(ScanResult(last_utime=since + 5), since)
    assert monitor.health.state("onchain").failing_since is None


# -- logging / entrypoint -------------------------------------------------------------
def test_configure_logging_with_rotating_file(tmp_path):
    log_file = str(tmp_path / "bot.log")
    configure_logging(log_file)
    logging.getLogger("grambot.test").info("hello file")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "hello file" in open(log_file, encoding="utf-8").read()
    configure_logging()  # back to stdout only; must not raise


def test_run_healthcheck_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("HEARTBEAT_PATH", str(tmp_path / "hb"))
    assert app_module.run(["--healthcheck"]) == 1
    write_heartbeat(str(tmp_path / "hb"))
    assert app_module.run(["--healthcheck"]) == 0


def test_run_uses_supervisor_by_default():
    with patch.object(app_module.Supervisor, "run", return_value=7) as run:
        assert app_module.run(["--no-telegram"]) == 7
    run.assert_called_once()


# -- telegram client ---------------------------------------------------------------------
class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_telegram_flood_wait_and_parse_fallback():
    notifier = TelegramNotifier("token", "1")
    responses = iter([
        _Resp({"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 1}}),
        _Resp({"ok": True, "result": {}}),
    ])
    sleeps = []
    with patch.object(notifier._session, "post", side_effect=lambda *a, **k: next(responses)), patch(
        "grambot.notifier.time.sleep", sleeps.append
    ):
        assert notifier.send("hi") is True
    assert sleeps == [1.0]

    calls = []

    def post(url, json=None, timeout=None):
        calls.append(json)
        if json.get("parse_mode") == "HTML":
            return _Resp({"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities"})
        return _Resp({"ok": True, "result": {}})

    with patch.object(notifier._session, "post", side_effect=post):
        assert notifier.send("<b>x</b> &amp; y") is True
    assert len(calls) == 2 and calls[1]["text"] == "x & y" and "parse_mode" not in calls[1]

    with patch.object(notifier._session, "post", return_value=_Resp({"ok": False, "error_code": 400, "description": "Bad Request: chat not found"})):
        assert notifier.send("hi") is False
        assert notifier.get_me() is None
    assert notifier.last_error_code == 400

    with patch.object(notifier._session, "post", return_value=_Resp({"ok": True, "result": {"username": "gram_bot"}})):
        assert notifier.get_me() == "gram_bot"
