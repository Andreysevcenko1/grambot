import calendar
from unittest.mock import MagicMock, patch

import requests

from grambot.config import DEFAULT_RSS_FEEDS, DEFAULT_TRUSTED_SOURCES
from grambot.sources.rss import fetch_feed
from grambot.sources.telegram_web import (
    channel_from_post_url,
    channel_from_url,
    fetch_channel,
    parse_channel_page,
)

PAGE = """<!DOCTYPE html><html><head>
<meta property="og:title" content="TON Status">
</head><body>
<div class="tgme_channel_info_header_title"><span dir="auto">TON Status</span></div>
<section class="tgme_channel_history js-message_history">
<div class="tgme_widget_message_wrap js-widget_message_wrap">
 <div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="tonstatus/240" data-view="x">
  <div class="tgme_widget_message_text js-message_text" dir="auto"><b>Mainnet Validators</b><br/><br/>Please schedule time
   tomorrow &amp; vote: <a href="https://t.me/tonstatus/1">details</a></div>
  <span class="tgme_widget_message_meta"><a class="tgme_widget_message_date" href="https://t.me/tonstatus/240">
   <time datetime="2026-08-26T12:53:00+00:00" class="time">12:53</time></a></span>
 </div>
</div>
<div class="tgme_widget_message_wrap js-widget_message_wrap">
 <div class="tgme_widget_message js-widget_message" data-post="tonstatus/241">
  <a class="tgme_widget_message_photo_wrap" href="https://t.me/tonstatus/241?single"></a>
  <time datetime="2026-08-27T10:00:00+00:00" class="time">10:00</time>
 </div>
</div>
<div class="tgme_widget_message_wrap js-widget_message_wrap">
 <div class="tgme_widget_message js-widget_message" data-post="tonstatus/242">
  <div class="tgme_widget_message_text js-message_text" dir="auto">Network is back to normal 🎉</div>
  <time datetime="2026-08-27T15:01:31+03:00" class="time">15:01</time>
 </div>
</div>
</section></body></html>"""

EMPTY_PAGE = """<html><head><meta property="og:title" content="Telegram: Contact @nobody"></head>
<body><div class="tgme_page_title">Telegram</div>
<div class="tgme_page_description">If you have Telegram, you can contact @nobody right away.</div></body></html>"""


def test_parse_channel_page_extracts_posts():
    result = parse_channel_page(PAGE, "tonstatus", "https://t.me/s/tonstatus", fetched_at=1000.0)
    assert result.ok
    assert result.feed_title == "TON Status - Telegram Channel"
    assert [item.url for item in result.items] == ["https://t.me/tonstatus/240", "https://t.me/tonstatus/242"]
    first, second = result.items
    assert first.source == "TON Status - Telegram Channel"
    assert first.title == "Mainnet Validators"
    assert first.summary == "Mainnet Validators Please schedule time tomorrow & vote: details"
    assert first.published_at == calendar.timegm((2026, 8, 26, 12, 53, 0, 0, 0, 0))
    assert second.title == "Network is back to normal 🎉"
    assert second.published_at == calendar.timegm((2026, 8, 27, 12, 1, 31, 0, 0, 0))  # +03:00 -> UTC


def test_parse_channel_page_uses_alias_for_source_but_canonical_post_urls():
    page = PAGE.replace('data-post="tonstatus/', 'data-post="ton_status_new/')
    result = parse_channel_page(page, "tonstatus", "https://t.me/s/tonstatus")
    assert result.ok
    assert result.items[0].url == "https://t.me/ton_status_new/240"
    assert result.items[0].source == "TON Status - Telegram Channel"


def test_parse_channel_page_reports_missing_preview():
    result = parse_channel_page(EMPTY_PAGE, "nobody", "https://t.me/s/nobody")
    assert not result.ok
    assert "unavailable" in result.error
    assert result.items == []


def test_parse_channel_page_falls_back_to_username_without_title():
    page = PAGE.replace('<meta property="og:title" content="TON Status">', "").replace(
        '<div class="tgme_channel_info_header_title"><span dir="auto">TON Status</span></div>', ""
    )
    result = parse_channel_page(page, "tonstatus", "https://t.me/s/tonstatus")
    assert result.feed_title == "tonstatus - Telegram Channel"


def test_channel_from_url_variants():
    assert channel_from_url("https://t.me/s/tonblockchain") == "tonblockchain"
    assert channel_from_url("t.me/durov/") == "durov"
    assert channel_from_url("@toncoin") == "toncoin"
    assert channel_from_url("https://telegram.me/s/telegram?before=100") == "telegram"
    assert channel_from_url("https://t.me/durov/123") is None  # a post, not a channel
    assert channel_from_url("https://t.me/joinchat/abc") is None
    assert channel_from_url("https://rsshub.example/telegram/channel/durov") is None
    assert channel_from_url("Official Channel") is None


def test_channel_from_post_url():
    assert channel_from_post_url("https://t.me/durov/123") == "durov"
    assert channel_from_post_url("https://t.me/s/durov") == "durov"
    assert channel_from_post_url("https://cointelegraph.com/news/x") is None
    assert channel_from_post_url("") is None


def test_fetch_channel_detects_redirect_to_profile():
    response = MagicMock(status_code=200, url="https://t.me/private_channel", text="<html></html>")
    session = MagicMock()
    session.get.return_value = response
    result = fetch_channel("private_channel", "https://t.me/s/private_channel", session=session)
    assert not result.ok
    assert "preview disabled" in result.error
    assert session.get.call_args.args[0] == "https://t.me/s/private_channel"


def test_fetch_channel_handles_network_errors():
    session = MagicMock()
    session.get.side_effect = requests.ConnectionError("boom")
    result = fetch_channel("durov", "https://t.me/s/durov", session=session)
    assert not result.ok
    assert "boom" in result.error


def test_fetch_feed_routes_telegram_urls_to_web_preview():
    response = MagicMock(status_code=200, url="https://t.me/s/tonstatus", text=PAGE)
    session = MagicMock()
    session.get.return_value = response
    with patch("grambot.sources.rss.feedparser.parse") as parse:
        result = fetch_feed("https://t.me/s/tonstatus", timeout=1, session=session)
    parse.assert_not_called()
    assert result.ok
    assert result.url == "https://t.me/s/tonstatus"
    assert len(result.items) == 2


def test_default_feeds_include_official_channels_without_rsshub():
    telegram_feeds = [url for url in DEFAULT_RSS_FEEDS if channel_from_url(url)]
    assert {channel_from_url(url) for url in telegram_feeds} >= {"tonblockchain", "tonstatus", "durov", "telegram"}
    assert not any("rsshub" in url for url in DEFAULT_RSS_FEEDS)
    # Every default channel is trusted by username, so renames do not break verification.
    trusted = {channel_from_url(entry) for entry in DEFAULT_TRUSTED_SOURCES}
    assert {channel_from_url(url) for url in telegram_feeds} <= trusted
