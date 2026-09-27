"""Market-activity helpers: hourly trading volume from public 5-minute
candles and the multi-window move detector.

Volume comes from exchange klines because aggregate providers only expose a
rolling 24h figure. Providers are tried in order (Binance public data mirror,
Binance, Bybit, OKX) with per-provider backoff; when none works the bot keeps
running without volume alerts.
"""
from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import requests

from .price import PriceMove, RateLimited, _get

logger = logging.getLogger(__name__)

BINANCE_VISION_KLINES_URL = "https://data-api.binance.vision/api/v3/klines"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
BYBIT_KLINES_URL = "https://api.bybit.com/v5/market/kline"
OKX_CANDLES_URL = "https://www.okx.com/api/v5/market/candles"

CANDLE_MINUTES = 5
CANDLES_PER_HOUR = 60 // CANDLE_MINUTES
CANDLE_LIMIT = 300  # 25 hours of 5-minute candles; also the OKX maximum
MIN_BASELINE_HOURS = 6
MAX_BASELINE_HOURS = 24
DEFAULT_BACKOFF_SECONDS = 600.0


@dataclass
class Candle:
    open_time: float  # seconds
    open: float
    close: float
    quote_volume: float  # traded value in the quote currency (USDT ~ USD)


@dataclass
class VolumeStats:
    volume_1h_usd: float  # traded value over the trailing hour
    baseline_hourly_usd: float  # median hourly value over the previous day
    ratio: Optional[float]  # volume_1h_usd / baseline_hourly_usd
    provider: str
    fetched_at: float
    hours_in_baseline: int


# -- klines fetchers -------------------------------------------------------
def _parse_rows(rows: Sequence[Sequence], time_idx: int, open_idx: int, close_idx: int, volume_idx: int) -> List[Candle]:
    candles = []
    for row in rows:
        try:
            candles.append(
                Candle(
                    open_time=float(row[time_idx]) / 1000.0,
                    open=float(row[open_idx]),
                    close=float(row[close_idx]),
                    quote_volume=float(row[volume_idx] or 0.0),
                )
            )
        except (IndexError, TypeError, ValueError):
            continue
    candles.sort(key=lambda c: c.open_time)
    return candles


def fetch_klines_binance(symbol: str, timeout: float = 10.0, url: str = BINANCE_KLINES_URL) -> List[Candle]:
    rows = _get(url, {"symbol": symbol, "interval": f"{CANDLE_MINUTES}m", "limit": CANDLE_LIMIT}, timeout).json()
    if not isinstance(rows, list):
        raise ValueError(f"unexpected klines payload: {str(rows)[:120]}")
    return _parse_rows(rows, time_idx=0, open_idx=1, close_idx=4, volume_idx=7)


def fetch_klines_bybit(symbol: str, timeout: float = 10.0) -> List[Candle]:
    payload = _get(
        BYBIT_KLINES_URL,
        {"category": "spot", "symbol": symbol, "interval": str(CANDLE_MINUTES), "limit": CANDLE_LIMIT},
        timeout,
    ).json()
    if payload.get("retCode") != 0:
        raise ValueError(f"bybit retCode {payload.get('retCode')}: {payload.get('retMsg')}")
    rows = payload.get("result", {}).get("list") or []
    return _parse_rows(rows, time_idx=0, open_idx=1, close_idx=4, volume_idx=6)


def fetch_klines_okx(symbol: str, timeout: float = 10.0) -> List[Candle]:
    inst = symbol if "-" in symbol else symbol.replace("USDT", "-USDT")
    payload = _get(OKX_CANDLES_URL, {"instId": inst, "bar": f"{CANDLE_MINUTES}m", "limit": CANDLE_LIMIT}, timeout).json()
    if payload.get("code") != "0":
        raise ValueError(f"okx code {payload.get('code')}: {payload.get('msg')}")
    return _parse_rows(payload.get("data") or [], time_idx=0, open_idx=1, close_idx=4, volume_idx=6)


# -- statistics ------------------------------------------------------------
def volume_stats(candles: Sequence[Candle], provider: str, now: Optional[float] = None) -> Optional[VolumeStats]:
    """Trailing-hour traded value vs the median hourly value of the day before.

    The trailing hour is the last ``CANDLES_PER_HOUR`` candles (the newest one
    is still forming, so the figure is slightly conservative). The baseline is
    the median of the preceding hourly sums, aligned to the same boundary, so
    a single earlier burst does not inflate it.
    """
    now = time.time() if now is None else now
    ordered = sorted((c for c in candles if c.open_time <= now + 60), key=lambda c: c.open_time)
    needed = CANDLES_PER_HOUR * (1 + MIN_BASELINE_HOURS)
    if len(ordered) < needed:
        return None
    recent = ordered[-CANDLES_PER_HOUR:]
    history = ordered[:-CANDLES_PER_HOUR]
    hourly: List[float] = []
    for end in range(len(history), CANDLES_PER_HOUR - 1, -CANDLES_PER_HOUR):
        chunk = history[end - CANDLES_PER_HOUR:end]
        hourly.append(sum(c.quote_volume for c in chunk))
        if len(hourly) >= MAX_BASELINE_HOURS:
            break
    if len(hourly) < MIN_BASELINE_HOURS:
        return None
    baseline = statistics.median(hourly)
    volume_1h = sum(c.quote_volume for c in recent)
    ratio = volume_1h / baseline if baseline > 0 else None
    return VolumeStats(
        volume_1h_usd=volume_1h,
        baseline_hourly_usd=baseline,
        ratio=ratio,
        provider=provider,
        fetched_at=now,
        hours_in_baseline=len(hourly),
    )


class VolumeClient:
    """Fetches hourly volume statistics through a provider chain.

    Results are cached for ``ttl`` seconds so the (relatively heavy) klines
    request is made a few times an hour at most. ``fetch`` never raises.
    """

    def __init__(self, symbol: str = "GRAMUSDT", timeout: float = 10.0, ttl: float = 300.0):
        self.symbol = symbol
        self.timeout = timeout
        self.ttl = ttl
        self.backoff_until: Dict[str, float] = {}
        self.last_provider: Optional[str] = None
        self.last_error: Optional[str] = None
        self.last_stats: Optional[VolumeStats] = None
        self.last_attempt_at: float = 0.0

    def providers(self) -> List[Tuple[str, Callable[[], List[Candle]]]]:
        return [
            ("binance-vision", lambda: fetch_klines_binance(self.symbol, self.timeout, BINANCE_VISION_KLINES_URL)),
            ("binance", lambda: fetch_klines_binance(self.symbol, self.timeout, BINANCE_KLINES_URL)),
            ("bybit", lambda: fetch_klines_bybit(self.symbol, self.timeout)),
            ("okx", lambda: fetch_klines_okx(self.symbol, self.timeout)),
        ]

    def fetch(self, force: bool = False) -> Optional[VolumeStats]:
        now = time.time()
        if not force and self.last_stats is not None and now - self.last_stats.fetched_at < self.ttl:
            return self.last_stats
        if not force and now - self.last_attempt_at < self.ttl:
            return None  # every provider failed recently; do not hammer them
        self.last_attempt_at = now
        for name, fetcher in self.providers():
            if self.backoff_until.get(name, 0.0) > now:
                continue
            try:
                candles = fetcher()
            except RateLimited as exc:
                self.backoff_until[name] = now + min(exc.retry_after, 3600.0)
                self.last_error = str(exc)
                logger.info("Volume provider %s rate limited; backing off %.0fs", name, exc.retry_after)
                continue
            except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
                # Geo-blocks (HTTP 451/403) land here too: skip the provider for a while.
                self.backoff_until[name] = now + DEFAULT_BACKOFF_SECONDS
                self.last_error = f"{name}: {exc}"
                logger.warning("Volume provider %s failed: %s", name, exc)
                continue
            stats = volume_stats(candles, name, now)
            if stats is None:
                self.last_error = f"{name}: not enough candles ({len(candles)})"
                logger.info("Volume provider %s returned too few candles (%d)", name, len(candles))
                continue
            if self.last_provider and self.last_provider != name:
                logger.info("Volume provider switched %s -> %s", self.last_provider, name)
            self.last_provider = name
            self.last_error = None
            self.last_stats = stats
            return stats
        return None


# -- detector helpers ------------------------------------------------------
def attach_volume(move: PriceMove, stats: Optional[VolumeStats]) -> PriceMove:
    if stats is not None:
        move.volume_1h_usd = stats.volume_1h_usd
        move.volume_1h_ratio = stats.ratio
        move.volume_provider = stats.provider
    return move


def triggered_windows(move: PriceMove, windows: Sequence[Tuple[int, float]]) -> List[Tuple[int, float, float]]:
    """Windows whose absolute change reached the threshold: (minutes, change, threshold)."""
    hits = []
    for minutes, threshold in windows:
        change = move.window_changes.get(minutes)
        if change is not None and abs(change) >= threshold:
            hits.append((minutes, change, threshold))
    return hits


def strongest_window(hits: Sequence[Tuple[int, float, float]]) -> Optional[Tuple[int, float, float]]:
    """The hit that exceeds its own threshold by the largest factor."""
    if not hits:
        return None
    return max(hits, key=lambda h: (abs(h[1]) / h[2] if h[2] > 0 else abs(h[1]), -h[0]))


def is_volume_spike(move: PriceMove, min_ratio: float, min_move_pct: float) -> bool:
    if move.volume_1h_ratio is None or move.volume_1h_ratio < min_ratio:
        return False
    change_1h = move.window_changes.get(60)
    if min_move_pct <= 0:
        return True
    return change_1h is not None and abs(change_1h) >= min_move_pct


def impulse(move: PriceMove, fast_minutes: int, fast_pct: float, slow_pct: float) -> Optional[Tuple[float, float]]:
    """Early-warning check: (fast change, 1h change) when the fast window moved
    at least ``fast_pct`` and the hourly change confirms the direction with at
    least ``slow_pct``; ``None`` otherwise.

    The hourly confirmation filters out a spike that merely retraces the
    previous half hour. ``slow_pct <= 0`` disables it.
    """
    fast = move.window_changes.get(fast_minutes)
    if fast is None or abs(fast) < fast_pct:
        return None
    slow = move.window_changes.get(60)
    if slow_pct > 0 and (slow is None or abs(slow) < slow_pct or slow * fast <= 0):
        return None
    return fast, (slow if slow is not None else fast)


__all__ = [
    "Candle",
    "VolumeClient",
    "VolumeStats",
    "attach_volume",
    "fetch_klines_binance",
    "fetch_klines_bybit",
    "fetch_klines_okx",
    "impulse",
    "is_volume_spike",
    "strongest_window",
    "triggered_windows",
    "volume_stats",
]
