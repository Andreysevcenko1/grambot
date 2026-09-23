from grambot.processing.filters import filter_relevant, is_relevant
from grambot.sources import NewsItem


def make_item(title: str, summary: str = "") -> NewsItem:
    return NewsItem(source="test", title=title, summary=summary, url=None, published_at=0.0)


def test_is_relevant_matches_keyword_case_insensitive():
    item = make_item("Toncoin hits new all-time high")
    assert is_relevant(item, ["ton"])


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
