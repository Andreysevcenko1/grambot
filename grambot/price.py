"""Price context with provider fallback.

Primary source is CoinGecko (aggregate market volume, no API key). When it
rate-limits (HTTP 429) or fails, public exchange tickers are tried in order:
Binance, Bybit, OKX. Volume figures differ between providers, so the
day-over-day volume ratio is only computed against a point from the same
provider.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import requests

from .storage import PricePoint, Storage

logger = logging.getLogger(__name__)

USER_AGENT = "grambot/0.2 (+https://github.com/Andreysevcenko1/grambot)"
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
BINANCE_URL = "https://api.binance.com/api/v3/ticker/24hr"
BYBIT_URL = "https://api.bybit.com/v5/market/tickers"
OKX_URL = "https://www.okx.com/api/v5/market/ticker"

DEFAULT_BACKOFF_SECONDS = 300.0


@dataclass
class PriceSnapshot:
    price_usd: float
    volume_24h_usd: float
    change_24h_pct: Optional[float]
    fetched_at: float
    provider: str = "coingecko"


@dataclass
class PriceMove:
    price_usd: float
    window_minutes: int
    window_change_pct: Optional[float]  # change over the configured window from local history
    change_24h_pct: Optional[float]  # 24h change reported by the provider
    volume_24h_usd: float
    volume_ratio_vs_yesterday: Optional[float]  # today's 24h volume / 24h volume a day ago
    provider: str = "coingecko"


class RateLimited(Exception):
    def __init__(self, provider: str, retry_after: float):
        super().__init__(f"{provider} rate limited, retry after {retry_after:.0f}s")
        self.provider = provider
        self.retry_after = retry_after


def _get(url: str, params: dict, timeout: float) -> requests.Response:
    response = requests.get(
        url, params=params, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=timeout
    )
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        try:
            wait = float(retry_after) if retry_after else DEFAULT_BACKOFF_SECONDS
        except ValueError:
            wait = DEFAULT_BACKOFF_SECONDS
        raise RateLimited(url, wait)
    response.raise_for_status()
    return response


def fetch_coingecko(coin_id: str, timeout: float = 10.0) -> Optional[PriceSnapshot]:
    payload = _get(
        COINGECKO_URL,
        {"ids": coin_id, "vs_currencies": "usd", "include_24hr_vol": "true", "include_24hr_change": "true"},
        timeout,
    ).json()
    if not isinstance(payload, dict):
        return None
    if payload.get("status", {}).get("error_code") == 429:
        raise RateLimited("coingecko", DEFAULT_BACKOFF_SECONDS)
    data = payload.get(coin_id)
    if not data or "usd" not in data:
        logger.warning("CoinGecko returned no data for %s: %r", coin_id, payload)
        return None
    change = data.get("usd_24h_change")
    return PriceSnapshot(
        price_usd=float(data["usd"]),
        volume_24h_usd=float(data.get("usd_24h_vol") or 0.0),
        change_24h_pct=float(change) if change is not None else None,
        fetched_at=time.time(),
        provider="coingecko",
    )


def fetch_binance(symbol: str, timeout: float = 10.0) -> Optional[PriceSnapshot]:
    data = _get(BINANCE_URL, {"symbol": symbol}, timeout).json()
    if "lastPrice" not in data:
        return None
    price = float(data["lastPrice"])
    if price <= 0 or float(data.get("bidPrice") or 0) <= 0:
        return None  # a delisted / halted pair reports a frozen price with empty book
    pct = data.get("priceChangePercent")
    return PriceSnapshot(
        price_usd=price,
        volume_24h_usd=float(data.get("quoteVolume") or 0.0),
        change_24h_pct=float(pct) if pct not in (None, "") else None,
        fetched_at=time.time(),
        provider="binance",
    )


def fetch_bybit(symbol: str, timeout: float = 10.0) -> Optional[PriceSnapshot]:
    payload = _get(BYBIT_URL, {"category": "spot", "symbol": symbol}, timeout).json()
    items = payload.get("result", {}).get("list") or []
    if payload.get("retCode") != 0 or not items:
        return None
    data = items[0]
    pct = data.get("price24hPcnt")
    return PriceSnapshot(
        price_usd=float(data["lastPrice"]),
        volume_24h_usd=float(data.get("turnover24h") or 0.0),
        change_24h_pct=float(pct) * 100 if pct not in (None, "") else None,
        fetched_at=time.time(),
        provider="bybit",
    )


def fetch_okx(symbol: str, timeout: float = 10.0) -> Optional[PriceSnapshot]:
    inst = symbol if "-" in symbol else symbol.replace("USDT", "-USDT")
    payload = _get(OKX_URL, {"instId": inst}, timeout).json()
    items = payload.get("data") or []
    if payload.get("code") != "0" or not items:
        return None
    data = items[0]
    last = float(data["last"])
    open_24h = float(data.get("open24h") or 0.0)
    change = (last - open_24h) / open_24h * 100 if open_24h > 0 else None
    return PriceSnapshot(
        price_usd=last,
        volume_24h_usd=float(data.get("volCcy24h") or 0.0),
        change_24h_pct=change,
        fetched_at=time.time(),
        provider="okx",
    )


class PriceClient:
    """Tries providers in order, remembering rate-limit backoffs per provider."""

    def __init__(self, coin_id: str = "the-open-network", symbol: str = "GRAMUSDT", timeout: float = 10.0):
        self.coin_id = coin_id
        self.symbol = symbol
        self.timeout = timeout
        self.backoff_until: Dict[str, float] = {}
        self.last_provider: Optional[str] = None
        self.last_error: Optional[str] = None

    def providers(self) -> List[Tuple[str, Callable[[], Optional[PriceSnapshot]]]]:
        return [
            ("coingecko", lambda: fetch_coingecko(self.coin_id, self.timeout)),
            ("binance", lambda: fetch_binance(self.symbol, self.timeout)),
            ("bybit", lambda: fetch_bybit(self.symbol, self.timeout)),
            ("okx", lambda: fetch_okx(self.symbol, self.timeout)),
        ]

    def fetch(self) -> Optional[PriceSnapshot]:
        now = time.time()
        for name, fetcher in self.providers():
            if self.backoff_until.get(name, 0.0) > now:
                continue
            try:
                snapshot = fetcher()
            except RateLimited as exc:
                self.backoff_until[name] = now + min(exc.retry_after, 3600.0)
                self.last_error = str(exc)
                logger.info("%s rate limited; backing off for %.0fs", name, exc.retry_after)
                continue
            except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
                self.last_error = f"{name}: {exc}"
                logger.warning("Price provider %s failed: %s", name, exc)
                continue
            if snapshot is not None:
                if self.last_provider and self.last_provider != name:
                    logger.info("Price provider switched %s -> %s", self.last_provider, name)
                self.last_provider = name
                self.last_error = None
                return snapshot
        return None


def fetch_price(coin_id: str, timeout: float = 10.0) -> Optional[PriceSnapshot]:
    """Single-shot fetch through the provider chain."""
    return PriceClient(coin_id=coin_id, timeout=timeout).fetch()


def pct_change(current: float, previous: Optional[float]) -> Optional[float]:
    if previous is None or previous <= 0:
        return None
    return (current - previous) / previous * 100.0


def compute_move(storage: Storage, snapshot: PriceSnapshot, window_minutes: int) -> PriceMove:
    """Combine the live snapshot with local history into a ``PriceMove``."""
    window_start = snapshot.fetched_at - window_minutes * 60
    earlier: Optional[PricePoint] = storage.price_at_or_before(window_start)
    window_change = pct_change(snapshot.price_usd, earlier.price_usd) if earlier else None

    day_ago = storage.price_near(snapshot.fetched_at - 86400, tolerance_seconds=3 * 3600)
    volume_ratio = None
    if (
        day_ago
        and day_ago.volume_usd > 0
        and snapshot.volume_24h_usd > 0
        and (day_ago.provider or "coingecko") == snapshot.provider
    ):
        volume_ratio = snapshot.volume_24h_usd / day_ago.volume_usd

    return PriceMove(
        price_usd=snapshot.price_usd,
        window_minutes=window_minutes,
        window_change_pct=window_change,
        change_24h_pct=snapshot.change_24h_pct,
        volume_24h_usd=snapshot.volume_24h_usd,
        volume_ratio_vs_yesterday=volume_ratio,
        provider=snapshot.provider,
    )


def move_from_history(storage: Storage, window_minutes: int) -> Optional[PriceMove]:
    """Build a ``PriceMove`` purely from stored data (no network call)."""
    latest = storage.latest_price()
    if latest is None:
        return None
    snapshot = PriceSnapshot(
        price_usd=latest.price_usd,
        volume_24h_usd=latest.volume_usd,
        change_24h_pct=latest.change_24h_pct,
        fetched_at=latest.fetched_at,
        provider=latest.provider or "coingecko",
    )
    return compute_move(storage, snapshot, window_minutes)


def is_spike(move: PriceMove, threshold_pct: float) -> bool:
    return move.window_change_pct is not None and abs(move.window_change_pct) >= threshold_pct
