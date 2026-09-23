from grambot.processing.filters import filter_relevant, is_relevant
from grambot.sources import NewsItem


def make_item(title: str, summary: str = "") -> NewsItem:
    return NewsItem(source="test", title=title, summary=summary, url=None, published_at=0.0)


def test_is_relevant_matches_keyword_case_insensitive():
    item = make_item("Toncoin hits new all-time high")
    assert is_relevant(item, ["Toncoin"])


def test_is_relevant_no_match():
    item = make_item("Ethereum upgrade announced")
    assert not is_relevant(item, ["TON", "Toncoin"])


def test_filter_relevant_keeps_only_matching_items():
    items = [
        make_item("Telegram launches new feature"),
        make_item("Unrelated stock market news"),
    ]
    result = filter_relevant(items, ["Telegram"])
    assert len(result) == 1
    assert result[0].title == "Telegram launches new feature"


def test_short_ticker_keyword_matches_uppercase_mention():
    item = make_item("TON Foundation confirms mainnet upgrade")
    assert is_relevant(item, ["TON"])


def test_short_ticker_keyword_ignores_common_word_weight_unit():
    item = make_item("A 50-ton truck rolled through the site")
    assert not is_relevant(item, ["TON"])


def test_short_ticker_keyword_ignores_substring_in_place_name():
    item = make_item("Ken-Ton schools ease Parker Boulevard congestion")
    assert not is_relevant(item, ["TON"])


def test_gram_keyword_ignores_weight_unit_and_matches_ticker():
    negative = make_item("Study finds each gram of salt increases risk")
    positive = make_item("Gram (GRAM) rises 3.1% amid market rally")
    assert not is_relevant(negative, ["GRAM"])
    assert is_relevant(positive, ["GRAM"])



def test_filter_fresh_drops_old_items():
    from grambot.processing.filters import filter_fresh

    now = 1_000_000.0
    fresh = NewsItem(source="s", title="a", summary="", url=None, published_at=now - 3600)
    stale = NewsItem(source="s", title="b", summary="", url=None, published_at=now - 48 * 3600)
    assert filter_fresh([fresh, stale], max_age_hours=24, now=now) == [fresh]


def test_multiword_keyword_matches_across_whitespace():
    item = make_item("Grants from the TON   Foundation announced")
    assert is_relevant(item, ["TON Foundation"])


def test_ticker_with_dollar_prefix_matches():
    item = make_item("$TON breaks out above resistance")
    assert is_relevant(item, ["TON"])
