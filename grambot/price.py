"""TON price/volume tracking via CoinGecko, with local history for
computing short-term % moves (e.g. "over the last 20 minutes") that the
public simple-price endpoint does not provide directly.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import requests

from .storage import Storage

logger = logging.getLogger(__name__)

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"


@dataclass
class PriceMove:
    price_usd: float
    volume_usd: float
    price_change_pct: Optional[float]
    volume_ratio: Optional[float]
    window_minutes: int


def fetch_price(coin_id: str, timeout: float = 10.0) -> Optional[tuple[float, float]]:
    try:
        response = requests.get(
            COINGECKO_URL,
            params={
                "ids": coin_id,
                "vs_currencies": "usd",
                "include_24hr_vol": "true",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        coin_data = data.get(coin_id)
        if not coin_data:
            return None
        return float(coin_data["usd"]), float(coin_data.get("usd_24h_vol", 0.0))
    except Exception:  # pragma: no cover - defensive, network dependent
        logger.exception("Failed to fetch price for %s", coin_id)
        return None


def poll_and_store(storage: Storage, coin_id: str) -> None:
    result = fetch_price(coin_id)
    if result is None:
        return
    price_usd, volume_usd = result
    storage.add_price_point(price_usd, volume_usd)


def recent_move(storage: Storage, window_minutes: int = 20) -> Optional[PriceMove]:
    latest = storage.latest_price()
    if latest is None:
        return None
    latest_price, latest_volume = latest

    past = storage.price_at_or_before(time.time() - window_minutes * 60)
    if past is None:
        return PriceMove(
            price_usd=latest_price,
            volume_usd=latest_volume,
            price_change_pct=None,
            volume_ratio=None,
            window_minutes=window_minutes,
        )

    past_price, past_volume = past
    price_change_pct = (
        ((latest_price - past_price) / past_price) * 100 if past_price else None
    )
    volume_ratio = (latest_volume / past_volume) if past_volume else None
    return PriceMove(
        price_usd=latest_price,
        volume_usd=latest_volume,
        price_change_pct=price_change_pct,
        volume_ratio=volume_ratio,
        window_minutes=window_minutes,
    )
