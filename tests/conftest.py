import os
import tempfile
import time
from typing import List

import pytest

from grambot.app import GramTonMonitor
from grambot.config import Settings
from grambot.processing.classifier import RuleBasedClassifier
from grambot.sources import NewsItem
from grambot.sources.rss import FeedResult
from grambot.storage import Storage


class FakeNotifier:
    def __init__(self, configured=True, fail=False):
        self.sent: List[str] = []
        self.sent_to: List[tuple] = []
        self.configured = configured
        self.fail = fail
        self.chat_id = "1"
        self.token = "t" if configured else ""

    @property
    def is_configured(self):
        return self.configured

    def send(self, text, disable_preview=True):
        if self.fail:
            return False
        self.sent.append(text)
        return True

    def send_to(self, chat_id, text, disable_preview=True):
        self.sent_to.append((chat_id, text))
        return self.send(text)

    def get_updates(self, offset, timeout_seconds=20):
        return []

    def set_commands(self, commands):
        return True

    def close(self):
        pass


class FakeSource:
    def __init__(self):
        self.batches: List[List[NewsItem]] = []
        self.last_results: List[FeedResult] = []

    def queue(self, items: List[NewsItem]):
        self.batches.append(items)

    def fetch_all(self):
        items = self.batches.pop(0) if self.batches else []
        self.last_results = [FeedResult(url="https://fake/feed", ok=True, items=items, feed_title="Fake")]
        return self.last_results

    def fetch(self):
        return [i for r in self.fetch_all() for i in r.items]


def make_item(title, source="Source A", minutes_ago=5, url=None, summary=""):
    return NewsItem(
        source=source,
        title=title,
        summary=summary,
        url=url or f"https://{source.replace(' ', '').lower()}.example/{abs(hash(title))}",
        published_at=time.time() - minutes_ago * 60,
    )


@pytest.fixture
def monitor():
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            telegram_bot_token="t",
            telegram_chat_id="1",
            database_path=os.path.join(tmp, "m.db"),
            keywords=["TON", "Toncoin", "GRAM", "Telegram"],
            trusted_sources=["Official Channel"],
            max_notifications_per_hour=3,
            price_alert_threshold_pct=5.0,
            price_alert_cooldown_minutes=60,
        )
        storage = Storage(settings.database_path)
        source = FakeSource()
        notifier = FakeNotifier()
        mon = GramTonMonitor(settings, storage, source, RuleBasedClassifier(), notifier, classifier_name="test")
        try:
            yield mon
        finally:
            storage.close()
