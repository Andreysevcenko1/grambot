"""People and assets to monitor; reports are not price predictions."""
from dataclasses import dataclass
from typing import List, Sequence, Tuple

from .processing.keywords import matches_any


@dataclass(frozen=True)
class Person:
    key: str
    name: str
    coins: str
    aliases: Tuple[str, ...]
    context: Tuple[str, ...]
    feeds: Tuple[str, ...] = ()


CRYPTO = ("crypto*", "bitcoin", "BTC", "blockchain*", "token*", "крипт*", "биткоин*", "блокчейн*")
PEOPLE = (
    Person("durov", "Павел Дуров", "TON", ("Pavel Durov", "Durov", "Павел Дуров", "Дуров"),
           ("TON", "Toncoin", "GRAM", "The Open Network", "кошел*", "wallet*", "crypto*", "крипт*"),
           ("https://t.me/s/durov",)),
    Person("musk", "Илон Маск", "DOGE", ("Elon Musk", "Илон Маск", "Маск"),
           ("DOGE", "Dogecoin", "Doge", *CRYPTO)),
    Person("trump", "Дональд Трамп", "BTC / TRUMP", ("Donald Trump", "President Trump", "Trump", "Дональд Трамп", "Трамп"),
           ("TRUMP", "WLFI", "stablecoin*", "резерв*", *CRYPTO),
           ("https://trumpstruth.org/feed",)),
    Person("cz", "Чанпэн Чжао", "BNB", ("Changpeng Zhao", "CZ", "Чанпэн Чжао"),
           ("BNB", "Binance", *CRYPTO)),
    Person("vitalik", "Виталик Бутерин", "ETH", ("Vitalik Buterin", "Vitalik", "Виталик Бутерин", "Бутерин"),
           ("ETH", "Ethereum", "эфириум*", *CRYPTO),
           ("https://vitalik.eth.limo/feed.xml",)),
    Person("sun", "Джастин Сан", "TRX", ("Justin Sun", "Джастин Сан"),
           ("TRX", "Tron", "TRON", *CRYPTO)),
    Person("yakovenko", "Анатолий Яковенко", "SOL", ("Anatoly Yakovenko", "Анатолий Яковенко", "Yakovenko"),
           ("SOL", "Solana", "солан*", *CRYPTO)),
    Person("hoskinson", "Чарльз Хоскинсон", "ADA", ("Charles Hoskinson", "Hoskinson", "Чарльз Хоскинсон"),
           ("ADA", "Cardano", "кардано", *CRYPTO)),
    Person("garlinghouse", "Брэд Гарлингхаус", "XRP", ("Brad Garlinghouse", "Garlinghouse", "Брэд Гарлингхаус"),
           ("XRP", "Ripple", "риппл", *CRYPTO)),
    Person("saylor", "Майкл Сэйлор", "BTC", ("Michael Saylor", "Saylor", "Майкл Сэйлор"),
           ("BTC", "Bitcoin", "биткоин*", *CRYPTO)),
)

AGGREGATOR_FEEDS = (
    "https://t.me/s/WatcherGuru",
    "https://t.me/s/wublockchainenglish",
    "https://t.me/s/cointelegraph",
)
BINANCE_FEED = "https://t.me/s/binance_announcements"
UPBIT_FEED = "https://api-manager.upbit.com/api/v1/announcements?os=web&page=1&per_page=20&category=trade"
OKX_FEED = "https://www.okx.com/api/v5/support/announcements?annType=announcements-new-listings"


def active_people(disabled: Sequence[str]) -> List[Person]:
    excluded = {key.lower() for key in disabled}
    unknown = excluded - {person.key for person in PEOPLE}
    if unknown:
        raise ValueError("Unknown WATCHLIST_DISABLE entries: " + ", ".join(sorted(unknown)))
    return [person for person in PEOPLE if person.key not in excluded]


def feed_urls(people: Sequence[Person], aggregators: Sequence[str]) -> List[str]:
    return list(dict.fromkeys([feed for person in people for feed in person.feeds]
                              + list(aggregators) + [BINANCE_FEED, UPBIT_FEED, OKX_FEED]))


def match_people(text: str, feed_url: str, people: Sequence[Person]) -> List[Person]:
    return [
        person for person in people
        if matches_any(text, person.context)
        and (feed_url in person.feeds or matches_any(text, person.aliases))
    ]
