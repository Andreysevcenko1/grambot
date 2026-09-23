import calendar
from unittest.mock import patch

import requests

from grambot.sources.rss import RSSSource, fetch_feed, parse_feed, strip_html

GOOGLE_NEWS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>"TON" - Google News</title>
<item><title>Toncoin Jumps 36% as Telegram Takes Over TON Chain - CoinMarketCap</title>
<link>https://news.google.com/x</link>
<pubDate>Tue, 22 Sep 2026 10:00:00 GMT</pubDate>
<description>&lt;a href="x"&gt;Toncoin Jumps&lt;/a&gt;&amp;nbsp;CoinMarketCap</description>
<source url="https://coinmarketcap.com">CoinMarketCap</source></item>
<item><title>TON Surges - Yahoo Finance</title><link>https://news.google.com/y</link>
<source url="https://finance.yahoo.com">Yahoo Finance</source></item>
</channel></rss>"""

PLAIN = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Cointelegraph.com News</title>
<item><title>TON news &amp; more</title><link>https://ct.example/1</link>
<description><![CDATA[<p>Hello <b>world</b></p>]]></description></item>
</channel></rss>"""


def test_parse_google_news_extracts_publisher_and_strips_suffix():
    result = parse_feed(GOOGLE_NEWS, "https://news.google.com/rss/search?q=TON", fetched_at=123.0)
    assert result.ok
    assert result.feed_title == '"TON" - Google News'
    first, second = result.items
    assert first.source == "CoinMarketCap"
    assert first.title == "Toncoin Jumps 36% as Telegram Takes Over TON Chain"
    assert first.published_at == calendar.timegm((2026, 9, 22, 10, 0, 0, 0, 0, 0))
    assert second.source == "Yahoo Finance"
    assert second.title == "TON Surges"
    assert second.published_at == 123.0  # no date -> fetch time


def test_parse_plain_feed_uses_feed_title_and_strips_html():
    result = parse_feed(PLAIN, "https://cointelegraph.com/rss")
    assert result.ok
    item = result.items[0]
    assert item.source == "Cointelegraph.com News"
    assert item.title == "TON news & more"
    assert item.summary == "Hello world"


def test_parse_garbage_reports_error():
    result = parse_feed(b"<html><body>not a feed</body>", "https://example.com/feed")
    assert not result.ok
    assert result.error
    assert result.items == []


def test_fetch_feed_handles_network_errors():
    with patch("grambot.sources.rss.requests.get", side_effect=requests.ConnectionError("boom")):
        result = fetch_feed("https://example.com/feed", timeout=1)
    assert not result.ok
    assert "boom" in result.error


def test_rss_source_fetches_all_in_configured_order():
    calls = []

    def fake_fetch(url, timeout, session):
        calls.append(url)
        return parse_feed(PLAIN if "b" in url else GOOGLE_NEWS, url)

    source = RSSSource(["https://a", "https://b"], timeout=3)
    with patch("grambot.sources.rss.fetch_feed", side_effect=fake_fetch):
        items = source.fetch()
    assert sorted(calls) == ["https://a", "https://b"]
    assert [r.url for r in source.last_results] == ["https://a", "https://b"]
    assert len(items) == 3


def test_strip_html():
    assert strip_html("<p>a&amp;b</p>  <br/>c") == "a&b c"
    assert strip_html("") == ""
