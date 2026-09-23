"""GRAM/TON news monitor bot.

This package implements a small pipeline that:

1. Collects items from RSS/Atom feeds (official blogs, Telegram/Twitter
   RSS bridges, etc.).
2. Filters them by keyword relevance.
3. Classifies sentiment and estimates a signal strength.
4. Clusters similar items together to count corroborating sources and
   avoid duplicate notifications.
5. Checks recent TON price/volume movement.
6. Sends a formatted notification to a Telegram chat.
"""

__version__ = "0.1.0"
