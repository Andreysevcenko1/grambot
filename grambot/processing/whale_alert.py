"""Whale Alert posts (``@whale_alert_io`` on Telegram) as large-transfer signals.

Whale Alert publishes lines such as::

    🚨 🚨 🚨 12,000,000 $TON (30,120,000 USD) transferred from #Binance to unknown wallet Details

They are a free stand-in for on-chain scanning: the parser turns them into the
same ``Transfer`` objects the on-chain module produces so the existing whale
alert (labels, deposit/withdrawal semantics, cooldown, ``/whales``) is reused.
Non-transfer posts (news headlines) are left to the news pipeline.
"""
from __future__ import annotations

import re
from typing import Optional

from ..onchain import Label, Transfer, classify_transfer
from ..sources import NewsItem

WHALE_ALERT_SOURCE = "Whale Alert"
TRACKED_SYMBOLS = {"TON", "GRAM"}

# Entities Whale Alert tags that are centralised exchanges (lower-case).
EXCHANGES = {
    "binance", "coinbase", "coinbase institutional", "okx", "okex", "bybit", "kraken", "bitget", "kucoin",
    "htx", "huobi", "upbit", "bithumb", "gate.io", "gate", "mexc", "bitfinex", "crypto.com", "gemini",
    "bitstamp", "bitmart", "bingx", "whitebit", "lbank", "poloniex", "coinone", "korbit", "bitvavo",
    "robinhood", "cex.io", "deribit", "hyperliquid",
}
FUNDS = {"treasury", "foundation", "capital", "fund", "ventures", "grayscale", "blackrock", "microstrategy", "strategy"}
DEXES = {"uniswap", "aave", "curve", "ston.fi", "stonfi", "dedust", "pancakeswap", "sushiswap", "1inch", "jupiter", "raydium"}

_TRANSFER_RE = re.compile(
    r"(?P<amount>\d[\d,]*(?:\.\d+)?)\s*\$(?P<symbol>[A-Za-z0-9]{2,12})\s*"
    r"\(\s*(?P<usd>\d[\d,]*(?:\.\d+)?)\s*USD\s*\)\s*"
    r"(?P<verb>transferred|minted|burned|locked|unlocked)"
    r"(?:\s+from\s+(?P<src>.+?))?\s+(?:to|at)\s+(?P<dst>.+?)"
    r"(?:\s+Details\b.*)?$",
    re.IGNORECASE | re.DOTALL,
)
_UNKNOWN_RE = re.compile(r"^unknown(\s+wallet)?$", re.IGNORECASE)


def _number(text: str) -> float:
    return float(text.replace(",", ""))


def _clean_party(text: Optional[str]) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.replace("#", "")).strip(" .")


def _label_for(name: str) -> Optional[Label]:
    """Whale Alert names a counterparty by entity, not address; map it onto the
    label categories the on-chain classifier understands."""
    if not name or _UNKNOWN_RE.match(name):
        return None
    lowered = name.lower()
    if lowered in EXCHANGES or any(lowered.startswith(ex + " ") for ex in EXCHANGES):
        return Label(name=name, category="CEX")
    if lowered in DEXES:
        return Label(name=name, category="DEX")
    if any(word in lowered for word in FUNDS):
        return Label(name=name, category="fund")
    # "Unknown Whale 1", "Whale 0x…" etc.: known to Whale Alert but not an exchange.
    return Label(name=name, category="other")


def is_whale_alert_item(item: NewsItem) -> bool:
    return WHALE_ALERT_SOURCE.lower() in (item.source or "").lower()


def parse_transfer(item: NewsItem) -> Optional[Transfer]:
    """``Transfer`` for a Whale Alert transfer post about a tracked coin; ``None``
    for other coins, news posts or unparseable text."""
    text = f"{item.title} {item.summary}".strip()
    match = _TRANSFER_RE.search(text)
    if not match:
        return None
    symbol = match.group("symbol").upper()
    if symbol not in TRACKED_SYMBOLS:
        return None
    try:
        amount = _number(match.group("amount"))
        usd = _number(match.group("usd"))
    except ValueError:
        return None
    if amount <= 0:
        return None
    verb = match.group("verb").lower()
    source_name = _clean_party(match.group("src"))
    destination_name = _clean_party(match.group("dst"))
    if verb != "transferred":
        # mint/burn/lock: keep the actor as the only known party.
        source_name, destination_name = (destination_name, "") if verb in ("minted", "unlocked") else ("", destination_name)
    source_label = _label_for(source_name)
    destination_label = _label_for(destination_name)
    kind = classify_transfer(source_label, destination_label) if verb == "transferred" else "fund"
    post_id = (item.url or "").rstrip("/").rsplit("/", 1)[-1] or item.item_hash[:16]
    transfer = Transfer(
        hash=f"whale-alert:{post_id}",
        utime=item.published_at,
        amount_ton=amount,
        source=source_name or "unknown",
        destination=destination_name or "unknown",
        source_label=source_label,
        destination_label=destination_label,
        kind=kind,
        link=item.url or "",
        usd_value=usd,
    )
    return transfer


def is_transfer_post(item: NewsItem) -> bool:
    """True for any Whale Alert transfer line (any coin) — these must not enter
    the news pipeline, where they would look like unverifiable headlines."""
    return bool(_TRANSFER_RE.search(f"{item.title} {item.summary}"))
