import os
import tempfile
import time
from unittest.mock import patch

import pytest
import requests

from grambot import price
from grambot.storage import Storage


@pytest.fixture
def storage():
    with tempfile.TemporaryDirectory() as tmp:
        store = Storage(os.path.join(tmp, "p.db"))
        try:
            yield store
        finally:
            store.close()


class _Resp:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._payload


BINANCE = {"symbol": "GRAMUSDT", "lastPrice": "1.405", "bidPrice": "1.404", "quoteVolume": "14528970.7", "priceChangePercent": "-3.037"}
BYBIT = {"retCode": 0, "result": {"list": [{"lastPrice": "1.406", "turnover24h": "7816598.9", "price24hPcnt": "-0.0290"}]}}
OKX = {"code": "0", "data": [{"last": "1.406", "open24h": "1.449", "volCcy24h": "5209763.8"}]}


def test_fetch_price_parses_coingecko_payload():
    payload = {"the-open-network": {"usd": 2.5, "usd_24h_vol": 1e8, "usd_24h_change": -3.2}}
    with patch("grambot.price.requests.get", return_value=_Resp(payload)):
        snap = price.fetch_price("the-open-network")
    assert snap.provider == "coingecko"
    assert snap.price_usd == 2.5
    assert snap.volume_24h_usd == 1e8
    assert snap.change_24h_pct == -3.2


def test_client_falls_back_to_exchanges_when_coingecko_rate_limited():
    responses = {
        price.COINGECKO_URL: _Resp({"status": {"error_code": 429}}, status_code=429, headers={"Retry-After": "60"}),
        price.BINANCE_URL: _Resp(BINANCE),
    }
    client = price.PriceClient()
    with patch("grambot.price.requests.get", side_effect=lambda url, **kw: responses[url]) as get:
        snap = client.fetch()
        assert snap.provider == "binance"
        assert snap.price_usd == 1.405
        assert snap.change_24h_pct == -3.037
        assert client.last_provider == "binance"
        assert client.backoff_until["coingecko"] > time.time()
        # While backing off, CoinGecko is not even attempted.
        client.fetch()
        assert all(call.args[0] != price.COINGECKO_URL for call in get.call_args_list[1:])


def test_client_skips_frozen_binance_pair_and_uses_bybit_then_okx():
    frozen = dict(BINANCE, bidPrice="0.00000000")
    responses = {
        price.COINGECKO_URL: _Resp({}),
        price.BINANCE_URL: _Resp(frozen),
        price.BYBIT_URL: _Resp(BYBIT),
        price.OKX_URL: _Resp(OKX),
    }
    client = price.PriceClient()
    with patch("grambot.price.requests.get", side_effect=lambda url, **kw: responses[url]):
        snap = client.fetch()
    assert snap.provider == "bybit"
    assert snap.change_24h_pct == pytest.approx(-2.9)

    responses[price.BYBIT_URL] = _Resp({"retCode": 10001, "result": {}})
    with patch("grambot.price.requests.get", side_effect=lambda url, **kw: responses[url]):
        snap = client.fetch()
    assert snap.provider == "okx"
    assert snap.change_24h_pct == pytest.approx((1.406 - 1.449) / 1.449 * 100)


def test_client_returns_none_when_everything_fails():
    client = price.PriceClient()
    with patch("grambot.price.requests.get", side_effect=requests.ConnectionError("offline")):
        assert client.fetch() is None
    assert "offline" in client.last_error


def test_volume_ratio_requires_same_provider(storage):
    now = 100_000.0
    storage.add_price_point(2.0, 1_000.0, fetched_at=now - 86400, provider="coingecko")
    snap = price.PriceSnapshot(price_usd=2.1, volume_24h_usd=2_000.0, change_24h_pct=None, fetched_at=now, provider="binance")
    assert price.compute_move(storage, snap, 20).volume_ratio_vs_yesterday is None


def test_compute_move_uses_history(storage):
    now = 100_000.0
    storage.add_price_point(2.0, 1_000.0, fetched_at=now - 86400)  # a day ago
    storage.add_price_point(2.0, 1_500.0, fetched_at=now - 25 * 60)  # before the 20-min window
    storage.add_price_point(2.2, 1_900.0, fetched_at=now - 5 * 60)
    snap = price.PriceSnapshot(price_usd=2.1, volume_24h_usd=2_000.0, change_24h_pct=5.0, fetched_at=now)

    move = price.compute_move(storage, snap, window_minutes=20)
    assert move.window_change_pct == pytest.approx(5.0)
    assert move.change_24h_pct == 5.0
    assert move.volume_ratio_vs_yesterday == pytest.approx(2.0)
    assert price.is_spike(move, 5.0)
    assert not price.is_spike(move, 5.1)


def test_compute_move_without_history(storage):
    snap = price.PriceSnapshot(price_usd=2.1, volume_24h_usd=0.0, change_24h_pct=None, fetched_at=1.0)
    move = price.compute_move(storage, snap, window_minutes=20)
    assert move.window_change_pct is None
    assert move.volume_ratio_vs_yesterday is None
    assert not price.is_spike(move, 1.0)
    assert price.move_from_history(storage, 20) is None


# -- fiat conversion ---------------------------------------------------------
def test_fiat_rates_use_market_usdt_rate_first_and_cache():
    with patch("grambot.price.requests.get", return_value=_Resp({"tether": {"eur": 0.8778}})) as get:
        rates = price.FiatRates()
        assert rates.get("EUR") == 0.8778
        assert rates.get("eur") == 0.8778  # cached, case-insensitive
        assert rates.get("USD") == 1.0
    assert get.call_count == 1
    assert get.call_args.kwargs["params"] == {"ids": "tether", "vs_currencies": "eur", "precision": "full"}


def test_fiat_rates_fall_back_to_erapi_then_frankfurter():
    def responder(url, **kw):
        if url == price.COINGECKO_URL:
            return _Resp({}, status_code=429, headers={"Retry-After": "60"})
        if url == price.ERAPI_URL:
            return _Resp({"result": "success", "rates": {"EUR": 0.8732}})
        raise AssertionError(url)

    with patch("grambot.price.requests.get", side_effect=responder):
        assert price.FiatRates().get("EUR") == 0.8732

    def only_frankfurter(url, **kw):
        if url == price.FRANKFURTER_URL:
            return _Resp({"base": "USD", "rates": {"RUB": 84.18}})
        raise requests.ConnectionError("down")

    with patch("grambot.price.requests.get", side_effect=only_frankfurter):
        assert price.FiatRates().get("RUB") == 84.18


def test_fiat_rates_return_stale_cache_when_all_providers_fail():
    rates = price.FiatRates(ttl=0.0)
    with patch("grambot.price.requests.get", return_value=_Resp({"tether": {"eur": 0.9}})):
        assert rates.get("EUR") == 0.9
    with patch("grambot.price.requests.get", side_effect=requests.ConnectionError("offline")):
        assert rates.get("EUR") == 0.9  # stale value re-used
        assert price.FiatRates().get("EUR") is None  # nothing cached -> None


def test_with_local_currency_converts_only_non_usd():
    move = price.PriceMove(1.406, 20, None, -3.0, 6.3e7, None)
    rates = price.FiatRates()
    rates._cache["EUR"] = (0.8732, time.time())
    price.with_local_currency(move, "eur", rates)
    assert move.local_currency == "EUR"
    assert round(move.local_price, 3) == 1.228
    price.with_local_currency(move, "USD", rates)
    assert move.local_currency == "USD" and move.local_price is None


def test_coingecko_quotes_display_currency_in_same_request():
    payload = {"the-open-network": {"usd": 1.4034, "eur": 1.2324, "usd_24h_vol": 6.3e7, "usd_24h_change": -3.0}}
    with patch("grambot.price.requests.get", return_value=_Resp(payload)) as get:
        snap = price.PriceClient(display_currency="eur").fetch()
    assert get.call_args.kwargs["params"]["vs_currencies"] == "usd,eur"
    assert snap.local_currency == "EUR" and snap.local_price == 1.2324

    with patch("grambot.price.requests.get", return_value=_Resp(payload)) as get:
        snap = price.PriceClient().fetch()
    assert get.call_args.kwargs["params"]["vs_currencies"] == "usd"
    assert snap.local_currency is None and snap.local_price is None


def test_quoted_local_price_wins_and_seeds_rate_cache(storage):
    snap = price.PriceSnapshot(1.4034, 6.3e7, -3.0, time.time(), local_currency="EUR", local_price=1.2324)
    move = price.compute_move(storage, snap, 20)
    rates = price.FiatRates()
    with patch("grambot.price.requests.get", side_effect=AssertionError("no network expected")):
        price.with_local_currency(move, "EUR", rates)
        assert move.local_price == 1.2324
        # An exchange snapshot (USD only) now converts with the implied rate.
        binance = price.PriceSnapshot(1.406, 1.4e7, -2.8, time.time(), provider="binance")
        move2 = price.with_local_currency(price.compute_move(storage, binance, 20), "EUR", rates)
    assert round(move2.local_price, 4) == round(1.406 * 1.2324 / 1.4034, 4)
