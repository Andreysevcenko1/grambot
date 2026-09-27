"""Perpetual-futures context: funding rate, open interest and liquidations.

Derivatives often move before spot: crowded longs show up as a high funding
rate, leverage piling in shows up as open interest (OI) growing fast, and a
cascade of liquidations shows up as OI collapsing while price gaps. None of
this predicts direction — the bot reports it as *information*.

Data comes from public endpoints, no keys: Bybit (one ticker call has
everything), Binance USDⓈ-M futures and OKX, tried in order with per-provider
backoff exactly like the volume client. Liquidation orders are only public on
OKX; Binance and Bybit expose them over websockets only, which the bot avoids.
Open-interest changes are computed from the bot's own history table, so they
are always compared within one provider.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

from .price import RateLimited, _get
from .storage import FuturesPoint, Storage

logger = logging.getLogger(__name__)

BINANCE_PREMIUM_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
BINANCE_OI_URL = "https://fapi.binance.com/fapi/v1/openInterest"
BINANCE_FUNDING_INFO_URL = "https://fapi.binance.com/fapi/v1/fundingInfo"
BYBIT_TICKERS_URL = "https://api.bybit.com/v5/market/tickers"
OKX_FUNDING_URL = "https://www.okx.com/api/v5/public/funding-rate"
OKX_OI_URL = "https://www.okx.com/api/v5/public/open-interest"
OKX_INSTRUMENTS_URL = "https://www.okx.com/api/v5/public/instruments"
OKX_LIQUIDATIONS_URL = "https://www.okx.com/api/v5/public/liquidation-orders"

DEFAULT_FUNDING_INTERVAL_HOURS = 8.0
DEFAULT_BACKOFF_SECONDS = 600.0
# Providers that keep failing (geo-blocked exchanges, e.g. Bybit/Binance from a
# US host) are retried less and less often, up to this pause.
MAX_BACKOFF_SECONDS = 6 * 3600.0
LIQUIDATION_WINDOW_SECONDS = 3600.0
OKX_LIQUIDATION_PAGE = 100  # the endpoint returns the latest 100 orders
STATIC_INFO_TTL = 6 * 3600.0  # funding intervals / contract sizes rarely change


@dataclass
class FuturesSnapshot:
    fetched_at: float
    provider: str
    symbol: str
    mark_price: Optional[float]
    funding_rate: Optional[float]  # fraction per funding interval (0.0001 = 0.01%)
    funding_interval_hours: float
    next_funding_at: Optional[float]
    open_interest: Optional[float]  # coins
    open_interest_usd: Optional[float]
    oi_changes: Dict[int, float] = field(default_factory=dict)  # minutes -> % change of open_interest

    @property
    def funding_pct(self) -> Optional[float]:
        return None if self.funding_rate is None else self.funding_rate * 100.0

    @property
    def funding_daily_pct(self) -> Optional[float]:
        if self.funding_rate is None or self.funding_interval_hours <= 0:
            return None
        return self.funding_rate * 100.0 * 24.0 / self.funding_interval_hours

    @property
    def funding_annual_pct(self) -> Optional[float]:
        daily = self.funding_daily_pct
        return None if daily is None else daily * 365.0


@dataclass
class LiquidationStats:
    provider: str
    fetched_at: float
    window_seconds: float
    total_usd: float
    long_usd: float
    short_usd: float
    count: int
    truncated: bool  # the page ended inside the window: totals are a lower bound

    @property
    def long_share(self) -> Optional[float]:
        return None if self.total_usd <= 0 else self.long_usd / self.total_usd


def _as_float(value) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ms_to_seconds(value) -> Optional[float]:
    number = _as_float(value)
    return None if number is None or number <= 0 else number / 1000.0


def _okx_data(payload: dict) -> list:
    if str(payload.get("code")) != "0":
        raise ValueError(f"okx code {payload.get('code')}: {payload.get('msg')}")
    return payload.get("data") or []


def base_asset(symbol: str) -> str:
    """``GRAMUSDT`` / ``GRAM-USDT-SWAP`` -> ``GRAM``."""
    head = symbol.split("-", 1)[0]
    for quote in ("USDT", "USDC", "USD"):
        if head.endswith(quote) and len(head) > len(quote):
            return head[: -len(quote)]
    return head or symbol


def okx_inst_id(symbol: str) -> str:
    """``GRAMUSDT`` -> ``GRAM-USDT-SWAP``; already-dashed ids pass through."""
    if "-" in symbol:
        return symbol if symbol.endswith("-SWAP") else f"{symbol}-SWAP"
    for quote in ("USDT", "USDC", "USD"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return f"{symbol[: -len(quote)]}-{quote}-SWAP"
    return f"{symbol}-USDT-SWAP"


# -- fetchers ----------------------------------------------------------------
class _StaticInfo:
    """Small TTL cache for per-symbol constants (funding interval, contract size)."""

    def __init__(self) -> None:
        self.values: Dict[str, float] = {}
        self.fetched_at: Dict[str, float] = {}

    def get(self, key: str, loader: Callable[[], Optional[float]], default: float) -> float:
        now = time.time()
        if now - self.fetched_at.get(key, 0.0) > STATIC_INFO_TTL:
            try:
                value = loader()
            except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
                logger.info("Could not refresh %s: %s", key, exc)
                value = None
            if value is not None:
                self.values[key] = value
            self.fetched_at[key] = now  # also on failure: do not retry every poll
        return self.values.get(key, default)


_static = _StaticInfo()


def _binance_funding_interval(symbol: str, timeout: float) -> float:
    def load() -> Optional[float]:
        rows = _get(BINANCE_FUNDING_INFO_URL, {}, timeout).json()
        for row in rows:
            if row.get("symbol") == symbol:
                return _as_float(row.get("fundingIntervalHours"))
        return DEFAULT_FUNDING_INTERVAL_HOURS  # symbols with the default interval are not listed

    return _static.get(f"binance-interval:{symbol}", load, DEFAULT_FUNDING_INTERVAL_HOURS)


def _okx_contract_value(inst_id: str, timeout: float) -> float:
    def load() -> Optional[float]:
        rows = _okx_data(_get(OKX_INSTRUMENTS_URL, {"instType": "SWAP", "instId": inst_id}, timeout).json())
        return _as_float(rows[0].get("ctVal")) if rows else None

    return _static.get(f"okx-ctval:{inst_id}", load, 1.0)


def fetch_binance(symbol: str, timeout: float = 10.0) -> FuturesSnapshot:
    premium = _get(BINANCE_PREMIUM_URL, {"symbol": symbol}, timeout).json()
    oi = _get(BINANCE_OI_URL, {"symbol": symbol}, timeout).json()
    mark = _as_float(premium.get("markPrice"))
    open_interest = _as_float(oi.get("openInterest"))
    if mark is None or open_interest is None:
        raise ValueError(f"binance: unexpected payload {str(premium)[:80]}")
    return FuturesSnapshot(
        fetched_at=time.time(),
        provider="binance",
        symbol=symbol,
        mark_price=mark,
        funding_rate=_as_float(premium.get("lastFundingRate")),
        funding_interval_hours=_binance_funding_interval(symbol, timeout),
        next_funding_at=_ms_to_seconds(premium.get("nextFundingTime")),
        open_interest=open_interest,
        open_interest_usd=open_interest * mark,
    )


def fetch_bybit(symbol: str, timeout: float = 10.0) -> FuturesSnapshot:
    payload = _get(BYBIT_TICKERS_URL, {"category": "linear", "symbol": symbol}, timeout).json()
    if payload.get("retCode") != 0:
        raise ValueError(f"bybit retCode {payload.get('retCode')}: {payload.get('retMsg')}")
    rows = (payload.get("result") or {}).get("list") or []
    if not rows:
        raise ValueError("bybit: empty ticker list")
    row = rows[0]
    mark = _as_float(row.get("markPrice")) or _as_float(row.get("lastPrice"))
    open_interest = _as_float(row.get("openInterest"))
    oi_usd = _as_float(row.get("openInterestValue"))
    if oi_usd is None and open_interest is not None and mark is not None:
        oi_usd = open_interest * mark
    return FuturesSnapshot(
        fetched_at=time.time(),
        provider="bybit",
        symbol=symbol,
        mark_price=mark,
        funding_rate=_as_float(row.get("fundingRate")),
        funding_interval_hours=_as_float(row.get("fundingIntervalHour")) or DEFAULT_FUNDING_INTERVAL_HOURS,
        next_funding_at=_ms_to_seconds(row.get("nextFundingTime")),
        open_interest=open_interest,
        open_interest_usd=oi_usd,
    )


def fetch_okx(symbol: str, timeout: float = 10.0) -> FuturesSnapshot:
    inst = okx_inst_id(symbol)
    funding_rows = _okx_data(_get(OKX_FUNDING_URL, {"instId": inst}, timeout).json())
    oi_rows = _okx_data(_get(OKX_OI_URL, {"instId": inst}, timeout).json())
    if not funding_rows or not oi_rows:
        raise ValueError(f"okx: no data for {inst}")
    funding, oi = funding_rows[0], oi_rows[0]
    this_time = _ms_to_seconds(funding.get("fundingTime"))
    prev_time = _ms_to_seconds(funding.get("prevFundingTime"))
    interval = (this_time - prev_time) / 3600.0 if this_time and prev_time and this_time > prev_time else DEFAULT_FUNDING_INTERVAL_HOURS
    open_interest = _as_float(oi.get("oiCcy")) or _as_float(oi.get("oi"))
    oi_usd = _as_float(oi.get("oiUsd"))
    mark = oi_usd / open_interest if oi_usd and open_interest else None
    return FuturesSnapshot(
        fetched_at=time.time(),
        provider="okx",
        symbol=symbol,
        mark_price=mark,
        funding_rate=_as_float(funding.get("fundingRate")),
        funding_interval_hours=interval,
        next_funding_at=this_time,
        open_interest=open_interest,
        open_interest_usd=oi_usd,
    )


def liquidation_stats(
    details: Sequence[dict],
    provider: str,
    window_seconds: float = LIQUIDATION_WINDOW_SECONDS,
    contract_value: float = 1.0,
    now: Optional[float] = None,
    page_size: int = OKX_LIQUIDATION_PAGE,
) -> LiquidationStats:
    """Aggregate OKX-style liquidation orders (``ts`` ms, ``sz`` contracts,
    ``bkPx`` bankruptcy price, ``posSide`` long/short) over the trailing window."""
    now = time.time() if now is None else now
    cutoff = now - window_seconds
    total = long_usd = short_usd = 0.0
    count = 0
    oldest: Optional[float] = None
    for order in details:
        ts = _ms_to_seconds(order.get("ts") or order.get("time"))
        if ts is None:
            continue
        oldest = ts if oldest is None else min(oldest, ts)
        if ts < cutoff:
            continue
        size = _as_float(order.get("sz")) or 0.0
        price = _as_float(order.get("bkPx")) or 0.0
        usd = size * contract_value * price
        if usd <= 0:
            continue
        count += 1
        total += usd
        if str(order.get("posSide", "")).lower() == "long" or str(order.get("side", "")).lower() == "sell":
            long_usd += usd
        else:
            short_usd += usd
    truncated = len(details) >= page_size and (oldest is None or oldest >= cutoff)
    return LiquidationStats(
        provider=provider,
        fetched_at=now,
        window_seconds=window_seconds,
        total_usd=total,
        long_usd=long_usd,
        short_usd=short_usd,
        count=count,
        truncated=truncated,
    )


def fetch_okx_liquidations(symbol: str, timeout: float = 10.0, window_seconds: float = LIQUIDATION_WINDOW_SECONDS) -> LiquidationStats:
    inst = okx_inst_id(symbol)
    underlying = inst[: -len("-SWAP")]
    payload = _get(OKX_LIQUIDATIONS_URL, {"instType": "SWAP", "uly": underlying, "state": "filled"}, timeout).json()
    rows = _okx_data(payload)
    details = rows[0].get("details") or [] if rows else []
    return liquidation_stats(details, "okx", window_seconds, contract_value=_okx_contract_value(inst, timeout))


# -- client ------------------------------------------------------------------
class FuturesClient:
    """Funding / open-interest snapshot through a provider chain, plus OKX
    liquidations. Results are cached for ``ttl`` seconds; nothing here raises."""

    def __init__(self, symbol: str = "GRAMUSDT", timeout: float = 10.0, ttl: float = 60.0):
        self.symbol = symbol
        self.timeout = timeout
        self.ttl = ttl
        self.backoff_until: Dict[str, float] = {}
        self.failures: Dict[str, int] = {}  # consecutive failures per provider
        self.last_provider: Optional[str] = None
        self.last_error: Optional[str] = None
        self.last_snapshot: Optional[FuturesSnapshot] = None
        self.last_attempt_at: float = 0.0
        self.last_liquidations: Optional[LiquidationStats] = None
        self.liquidations_error: Optional[str] = None
        self.liquidations_backoff_until: float = 0.0
        self.liquidations_attempt_at: float = 0.0
        self.liquidations_failures: int = 0

    @staticmethod
    def backoff_for(failures: int) -> float:
        """10 min after the first failure, doubling each time, capped at 6 h."""
        return min(DEFAULT_BACKOFF_SECONDS * (2 ** max(failures - 1, 0)), MAX_BACKOFF_SECONDS)

    def _provider_failed(self, name: str, exc: Exception, now: float) -> None:
        self.failures[name] = self.failures.get(name, 0) + 1
        pause = self.backoff_for(self.failures[name])
        self.backoff_until[name] = now + pause
        self.last_error = f"{name}: {exc}"
        # Warn once, then stay quiet: a geo-blocked exchange fails at every retry.
        log = logger.warning if self.failures[name] == 1 else logger.info
        log("Futures provider %s failed (%d in a row, next try in %.0f min): %s", name, self.failures[name], pause / 60, exc)

    def providers(self) -> List[Tuple[str, Callable[[], FuturesSnapshot]]]:
        return [
            ("bybit", lambda: fetch_bybit(self.symbol, self.timeout)),
            ("binance", lambda: fetch_binance(self.symbol, self.timeout)),
            ("okx", lambda: fetch_okx(self.symbol, self.timeout)),
        ]

    def fetch(self, force: bool = False) -> Optional[FuturesSnapshot]:
        now = time.time()
        if not force and self.last_snapshot is not None and now - self.last_snapshot.fetched_at < self.ttl:
            return self.last_snapshot
        if not force and now - self.last_attempt_at < self.ttl:
            return None
        self.last_attempt_at = now
        for name, fetcher in self.providers():
            if self.backoff_until.get(name, 0.0) > now:
                continue
            try:
                snapshot = fetcher()
            except RateLimited as exc:
                self.backoff_until[name] = now + min(exc.retry_after, 3600.0)
                self.last_error = str(exc)
                logger.info("Futures provider %s rate limited; backing off %.0fs", name, exc.retry_after)
                continue
            except (requests.RequestException, ValueError, TypeError, KeyError, IndexError) as exc:
                self._provider_failed(name, exc, now)
                continue
            if snapshot.open_interest is None and snapshot.funding_rate is None:
                self._provider_failed(name, ValueError("empty snapshot"), now)
                continue
            if self.last_provider and self.last_provider != name:
                logger.info("Futures provider switched %s -> %s", self.last_provider, name)
            self.failures[name] = 0
            self.last_provider = name
            self.last_error = None
            self.last_snapshot = snapshot
            return snapshot
        return None

    def fetch_liquidations(self, force: bool = False) -> Optional[LiquidationStats]:
        now = time.time()
        if not force and self.last_liquidations is not None and now - self.last_liquidations.fetched_at < self.ttl:
            return self.last_liquidations
        if not force and (now - self.liquidations_attempt_at < self.ttl or self.liquidations_backoff_until > now):
            return None
        self.liquidations_attempt_at = now
        try:
            stats = fetch_okx_liquidations(self.symbol, self.timeout)
        except RateLimited as exc:
            self.liquidations_backoff_until = now + min(exc.retry_after, 3600.0)
            self.liquidations_error = str(exc)
            return None
        except (requests.RequestException, ValueError, TypeError, KeyError, IndexError) as exc:
            self.liquidations_failures += 1
            pause = self.backoff_for(self.liquidations_failures)
            self.liquidations_backoff_until = now + pause
            self.liquidations_error = f"okx: {exc}"
            log = logger.warning if self.liquidations_failures == 1 else logger.info
            log("Liquidation data failed (%d in a row, next try in %.0f min): %s", self.liquidations_failures, pause / 60, exc)
            return None
        self.liquidations_failures = 0
        self.liquidations_error = None
        self.last_liquidations = stats
        return stats


# -- history / detector helpers ---------------------------------------------
def _reference_point(storage: Storage, ts: float, window_seconds: float, provider: str) -> Optional[FuturesPoint]:
    tolerance = max(window_seconds * 0.25, 300.0) + 60.0
    point = storage.futures_point_at_or_before(ts, provider)
    if point is not None and ts - point.fetched_at <= tolerance:
        return point
    return storage.futures_point_near(ts, tolerance, provider)


def oi_changes(storage: Storage, snapshot: FuturesSnapshot, windows_minutes: Iterable[int]) -> Dict[int, float]:
    """Percent change of open interest (in coins) over each window, using the
    bot's own history for the same provider. Windows without history are omitted."""
    changes: Dict[int, float] = {}
    if not snapshot.open_interest:
        return changes
    for minutes in sorted(set(windows_minutes)):
        seconds = minutes * 60.0
        reference = _reference_point(storage, snapshot.fetched_at - seconds, seconds, snapshot.provider)
        if reference is None or not reference.open_interest:
            continue
        changes[minutes] = (snapshot.open_interest - reference.open_interest) / reference.open_interest * 100.0
    return changes


def triggered_oi_windows(snapshot: FuturesSnapshot, windows: Sequence[Tuple[int, float]]) -> List[Tuple[int, float, float]]:
    """Windows whose absolute OI change reached the threshold: (minutes, change, threshold)."""
    hits = []
    for minutes, threshold in windows:
        change = snapshot.oi_changes.get(minutes)
        if change is not None and abs(change) >= threshold:
            hits.append((minutes, change, threshold))
    return hits


def strongest_oi_window(hits: Sequence[Tuple[int, float, float]]) -> Optional[Tuple[int, float, float]]:
    if not hits:
        return None
    return max(hits, key=lambda hit: (abs(hit[1]) / hit[2], -hit[0]))


def funding_is_extreme(snapshot: FuturesSnapshot, threshold_daily_pct: float) -> bool:
    daily = snapshot.funding_daily_pct
    return daily is not None and threshold_daily_pct > 0 and abs(daily) >= threshold_daily_pct


def positioning_note(oi_change_pct: float, price_change_pct: Optional[float]) -> str:
    """One-line reading of OI vs price (the classic four-quadrant table)."""
    if price_change_pct is None:
        return (
            "открытый интерес растёт — на рынок заходит новое плечо, растёт риск резкого движения"
            if oi_change_pct > 0
            else "открытый интерес падает — позиции закрывают или ликвидируют"
        )
    if abs(price_change_pct) < 0.5:
        return (
            "открытый интерес растёт при ровной цене — стороны набирают позиции, растёт риск резкого движения"
            if oi_change_pct > 0
            else "открытый интерес падает при ровной цене — позиции закрывают, рынок «остывает»"
        )
    if oi_change_pct > 0:
        return (
            "рост цены на растущем OI — новые лонги с плечом; движение подтверждено, но перегрев возможен"
            if price_change_pct > 0
            else "падение цены на растущем OI — открываются новые шорты; давление продавцов усиливается"
        )
    return (
        "рост цены на падающем OI — похоже на закрытие/ликвидацию шортов (шорт-сквиз)"
        if price_change_pct > 0
        else "падение цены на падающем OI — похоже на ликвидацию/закрытие лонгов"
    )


def funding_note(daily_pct: float) -> str:
    if daily_pct > 0:
        return "лонги платят шортам — рынок перегружен лонгами; такие периоды часто заканчиваются откатом, но не обязательно"
    return "шорты платят лонгам — рынок перегружен шортами; возможен шорт-сквиз, но не обязательно"
