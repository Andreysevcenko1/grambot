#!/usr/bin/env python3
"""Entry point for the GRAM/TON news monitor bot.

Usage:
    python main.py                # run forever (news + price + Telegram commands)
    python main.py --once         # single poll, useful for cron / smoke tests
    python main.py --no-telegram  # log messages instead of sending them
"""
import sys
import warnings

# macOS system Python links against LibreSSL; urllib3 warns about it on every
# start. It is harmless for our HTTPS calls, so keep the logs clean.
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

from grambot.app import run  # noqa: E402

if __name__ == "__main__":
    sys.exit(run())
