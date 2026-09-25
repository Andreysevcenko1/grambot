"""Tiny process supervisor so ``python main.py`` survives crashes on its own.

The parent process runs the bot as a child (``main.py --no-supervise ...``)
and restarts it whenever it exits with a non-zero code — an unhandled error,
the watchdog (hung loop / memory) or a crash of the interpreter itself.
Restarts back off exponentially so a persistent failure does not spin, and
the counter resets once the child has run for a while. A clean exit (code 0,
Ctrl+C, SIGTERM) stops the supervisor too, so Docker/systemd/launchd keep
their usual semantics.

The child learns about the situation through ``GRAMBOT_RESTART_COUNT`` and
``GRAMBOT_LAST_EXIT_CODE`` and mentions it in the startup message.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from typing import List, Optional, Sequence

logger = logging.getLogger("grambot.supervisor")

CHILD_FLAG = "--no-supervise"
BACKOFF_SCHEDULE = (5, 15, 30, 60, 120, 300)
STABLE_RUN_SECONDS = 600  # a child alive this long resets the failure streak
TERMINATE_GRACE_SECONDS = 30


def backoff_seconds(consecutive_failures: int) -> int:
    index = max(0, min(consecutive_failures, len(BACKOFF_SCHEDULE)) - 1)
    return BACKOFF_SCHEDULE[index]


def child_command(argv: Sequence[str]) -> List[str]:
    """Same interpreter, same entry script, plus the child flag."""
    script = os.path.abspath(sys.argv[0])
    args = [a for a in argv if a != CHILD_FLAG]
    return [sys.executable, script, CHILD_FLAG, *args]


class Supervisor:
    def __init__(self, argv: Sequence[str], env: Optional[dict] = None, sleep=time.sleep, popen=subprocess.Popen):
        self.argv = list(argv)
        self.env = dict(env if env is not None else os.environ)
        self.sleep = sleep
        self.popen = popen
        self.child: Optional[subprocess.Popen] = None
        self.stopping = False
        self.restarts = 0
        self.failures = 0

    # -- signals ----------------------------------------------------------
    def _forward(self, signum, frame) -> None:  # pragma: no cover - process signal
        self.stopping = True
        if self.child and self.child.poll() is None:
            logger.info("Supervisor got signal %s; stopping the bot", signum)
            try:
                self.child.send_signal(signal.SIGTERM)
            except OSError:
                pass

    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self._forward)
            except ValueError:  # pragma: no cover - not in main thread
                pass

    # -- loop -------------------------------------------------------------
    def spawn(self, last_exit: Optional[int]) -> subprocess.Popen:
        env = dict(self.env)
        env["GRAMBOT_RESTART_COUNT"] = str(self.failures)
        env["GRAMBOT_LAST_EXIT_CODE"] = "" if last_exit is None else str(last_exit)
        return self.popen(child_command(self.argv), env=env)

    def wait_child(self) -> int:
        assert self.child is not None
        try:
            return self.child.wait()
        except KeyboardInterrupt:  # pragma: no cover - Ctrl+C reaches both processes
            self.stopping = True
            try:
                return self.child.wait(TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                self.child.kill()
                return self.child.wait()

    def run(self) -> int:
        self.install_signal_handlers()
        last_exit: Optional[int] = None
        while not self.stopping:
            started = time.monotonic()
            self.child = self.spawn(last_exit)
            code = self.wait_child()
            uptime = time.monotonic() - started
            last_exit = code
            if self.stopping or code == 0:
                logger.info("Bot exited with code %d; supervisor done", code)
                return 0 if self.stopping else code
            if uptime >= STABLE_RUN_SECONDS:
                self.failures = 0
            self.failures += 1
            self.restarts += 1
            delay = backoff_seconds(self.failures)
            logger.error(
                "Bot exited with code %d after %.0fs (failure %d in a row); restarting in %ds",
                code, uptime, self.failures, delay,
            )
            self.sleep(delay)
        return 0


def is_child_process() -> bool:
    return CHILD_FLAG in sys.argv


def restart_info() -> tuple:
    """(restart_count, last_exit_code) passed in by the supervisor, if any."""
    try:
        count = int(os.environ.get("GRAMBOT_RESTART_COUNT", "0") or 0)
    except ValueError:
        count = 0
    raw = os.environ.get("GRAMBOT_LAST_EXIT_CODE", "")
    try:
        last = int(raw) if raw else None
    except ValueError:
        last = None
    return count, last
