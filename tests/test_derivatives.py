import logging
import time
from unittest.mock import patch

import pytest
import requests

from grambot import derivatives as d
from grambot.app import (
    FUTURES_FUNDING_FIRED_KEY,
    FUTURES_LIQ_FIRED_KEY,
    FUTURES_OI_FIRED_KEY,
    LAST_FUTURES_ALERT_KEY,
)
from grambot.commands import CommandHandler
from grambot.derivatives import FuturesClient, FuturesSnapshot, LiquidationStats
from grambot.notifier import format_futures_alert, format_futures_status
from grambot.price import RateLimited


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def fake_get(routes):
    def _get(url, params, timeout):
        for key, payload in routes.items():
            if key in url:
                return FakeResponse(payload(params) if callable(payload) else payload)
        raise AssertionError(f"unexpected url {url}")

    return _get


# -- payload parsing ----------------------------------------------------------
def test_fetch_bybit_parses_ticker():
    payload = {
        "retCode": 0,
        "result": {"list": [{
            "symbol": "GRAMUSDT", "markPrice": "2.5", "fundingRate": "0.0001", "fundingIntervalHour": "4",
            "openInterest": "40000000", "openInterestValue": "100000000", "nextFundingTime": "1790000000000",
        }]},
    }
    with patch.object(d, "_get", fake_get({"bybit": payload})):
        snap = d.fetch_bybit("GRAMUSDT")
    assert snap.provider == "bybit" and snap.mark_price == 2.5
    assert snap.funding_rate == 0.0001 and snap.funding_interval_hours == 4
    assert snap.funding_daily_pct == pytest.approx(0.06)
    assert snap.funding_annual_pct == pytest.approx(21.9)
    assert snap.open_interest == 40_000_000 and snap.open_interest_usd == 100_000_000
    assert snap.next_funding_at == 1_790_000_000


def test_fetch_bybit_rejects_error_code():
    with patch.object(d, "_get", fake_get({"bybit": {"retCode": 10001, "retMsg": "bad symbol", "result": {}}})):
        with pytest.raises(ValueError):
            d.fetch_bybit("NOPE")


def test_fetch_binance_uses_funding_interval_info():
    d._static.values.clear()
    d._static.fetched_at.clear()
    routes = {
        "premiumIndex": {"symbol": "GRAMUSDT", "markPrice": "2.50", "lastFundingRate": "-0.0002", "nextFundingTime": 1790000000000},
        "openInterest": {"symbol": "GRAMUSDT", "openInterest": "10000000"},
        "fundingInfo": [{"symbol": "GRAMUSDT", "fundingIntervalHours": 4}, {"symbol": "XUSDT", "fundingIntervalHours": 1}],
    }
    with patch.object(d, "_get", fake_get(routes)):
        snap = d.fetch_binance("GRAMUSDT")
    assert snap.provider == "binance" and snap.funding_interval_hours == 4
    assert snap.funding_daily_pct == pytest.approx(-0.12)
    assert snap.open_interest_usd == pytest.approx(25_000_000)
    # Unknown symbol -> default 8h interval, cached separately.
    routes["fundingInfo"] = []
    with patch.object(d, "_get", fake_get(routes)):
        assert d.fetch_binance("OTHERUSDT").funding_interval_hours == 8


def test_fetch_okx_derives_interval_and_price():
    d._static.values.clear()
    d._static.fetched_at.clear()
    routes = {
        "funding-rate": {"code": "0", "data": [{"fundingRate": "0.0003", "fundingTime": "1790014400000", "prevFundingTime": "1790000000000"}]},
        "open-interest": {"code": "0", "data": [{"oi": "8000000", "oiCcy": "8000000", "oiUsd": "20000000"}]},
    }
    with patch.object(d, "_get", fake_get(routes)):
        snap = d.fetch_okx("GRAMUSDT")
    assert snap.funding_interval_hours == pytest.approx(4.0)
    assert snap.mark_price == pytest.approx(2.5)
    assert snap.open_interest == 8_000_000 and snap.open_interest_usd == 20_000_000
    with patch.object(d, "_get", fake_get({"funding-rate": {"code": "51001", "msg": "Instrument ID does not exist"}, "open-interest": {}})):
        with pytest.raises(ValueError):
            d.fetch_okx("NOPEUSDT")


def test_okx_ids_and_base_asset():
    assert d.okx_inst_id("GRAMUSDT") == "GRAM-USDT-SWAP"
    assert d.okx_inst_id("GRAM-USDT") == "GRAM-USDT-SWAP"
    assert d.okx_inst_id("GRAM-USDT-SWAP") == "GRAM-USDT-SWAP"
    assert d.base_asset("GRAMUSDT") == "GRAM"
    assert d.base_asset("GRAM-USDT-SWAP") == "GRAM"
    assert d.base_asset("TONUSDC") == "TON"


def test_liquidation_stats_window_sides_and_truncation():
    now = 1_790_000_000.0
    details = [
        {"posSide": "long", "sz": "100000", "bkPx": "2.5", "ts": str(int((now - 600) * 1000))},   # $250k long
        {"posSide": "short", "sz": "20000", "bkPx": "2.5", "ts": str(int((now - 1200) * 1000))},  # $50k short
        {"posSide": "long", "sz": "999999", "bkPx": "2.5", "ts": str(int((now - 7200) * 1000))},  # outside the hour
    ]
    stats = d.liquidation_stats(details, "okx", now=now)
    assert stats.total_usd == pytest.approx(300_000)
    assert stats.long_usd == pytest.approx(250_000) and stats.short_usd == pytest.approx(50_000)
    assert stats.count == 2 and not stats.truncated
    assert stats.long_share == pytest.approx(250 / 300)
    # A full page whose oldest order is still inside the window: totals are a lower bound.
    page = [{"posSide": "long", "sz": "1000", "bkPx": "2.5", "ts": str(int((now - 30 * i) * 1000))} for i in range(100)]
    assert d.liquidation_stats(page, "okx", now=now).truncated
    assert d.liquidation_stats([], "okx", now=now).total_usd == 0


def test_fetch_okx_liquidations_scales_by_contract_value():
    d._static.values.clear()
    d._static.fetched_at.clear()
    now = time.time()
    routes = {
        "liquidation-orders": {"code": "0", "data": [{"details": [
            {"posSide": "short", "sz": "10", "bkPx": "2.0", "ts": str(int((now - 100) * 1000))},
        ]}]},
        "instruments": {"code": "0", "data": [{"instId": "GRAM-USDT-SWAP", "ctVal": "10"}]},
    }
    with patch.object(d, "_get", fake_get(routes)):
        stats = d.fetch_okx_liquidations("GRAMUSDT")
    assert stats.total_usd == pytest.approx(200.0) and stats.short_usd == pytest.approx(200.0)


# -- client ---------------------------------------------------------------------
def make_snapshot(provider="bybit", funding_rate=0.0001, oi=40_000_000.0, fetched_at=None, interval=4.0):
    return FuturesSnapshot(
        fetched_at=fetched_at or time.time(), provider=provider, symbol="GRAMUSDT", mark_price=2.5,
        funding_rate=funding_rate, funding_interval_hours=interval, next_funding_at=None,
        open_interest=oi, open_interest_usd=oi * 2.5 if oi else None,
    )


def test_client_falls_back_between_providers_and_backs_off():
    client = FuturesClient("GRAMUSDT", ttl=0.0)
    calls = []

    def bybit():
        calls.append("bybit")
        raise requests.ConnectionError("451")

    def binance():
        calls.append("binance")
        raise RateLimited("binance", 120)

    def okx():
        calls.append("okx")
        return make_snapshot("okx")

    with patch.object(client, "providers", return_value=[("bybit", bybit), ("binance", binance), ("okx", okx)]):
        snap = client.fetch(force=True)
        assert snap is not None and snap.provider == "okx" and client.last_provider == "okx"
        assert client.backoff_until["bybit"] > time.time() + 500  # default 10 min backoff
        assert time.time() + 100 < client.backoff_until["binance"] <= time.time() + 120  # Retry-After honoured
        calls.clear()
        assert client.fetch(force=True).provider == "okx"
        assert calls == ["okx"]  # failed providers are skipped while backing off


def test_client_returns_none_when_everything_fails_and_caches_snapshot():
    client = FuturesClient("GRAMUSDT", ttl=60.0)
    boom = lambda: (_ for _ in ()).throw(ValueError("bad payload"))
    with patch.object(client, "providers", return_value=[("bybit", boom)]):
        assert client.fetch(force=True) is None
        assert "bybit" in (client.last_error or "")
    good = make_snapshot()
    client.backoff_until.clear()  # the failure above put bybit into backoff
    with patch.object(client, "providers", return_value=[("bybit", lambda: good)]):
        assert client.fetch(force=True) is good
    with patch.object(client, "providers", return_value=[("bybit", boom)]):
        assert client.fetch() is good  # within ttl: cached, no network call


def test_liquidation_fetch_backs_off_after_failure():
    client = FuturesClient("GRAMUSDT", ttl=0.0)
    with patch.object(d, "fetch_okx_liquidations", side_effect=requests.ConnectionError("down")):
        assert client.fetch_liquidations(force=True) is None
        assert client.liquidations_backoff_until > time.time() and "okx" in client.liquidations_error
    stats = LiquidationStats("okx", time.time(), 3600, 1000.0, 1000.0, 0.0, 1, False)
    with patch.object(d, "fetch_okx_liquidations", return_value=stats):
        assert client.fetch_liquidations() is None  # still backing off
        assert client.fetch_liquidations(force=True) is stats
        assert client.liquidations_failures == 0


def test_repeated_provider_failures_back_off_exponentially(caplog):
    """A geo-blocked exchange must not be hammered (or logged) every 10 minutes."""
    assert FuturesClient.backoff_for(1) == 600.0
    assert FuturesClient.backoff_for(2) == 1200.0
    assert FuturesClient.backoff_for(4) == 4800.0
    assert FuturesClient.backoff_for(20) == d.MAX_BACKOFF_SECONDS

    client = FuturesClient("GRAMUSDT", ttl=0.0)
    blocked = lambda: (_ for _ in ()).throw(requests.HTTPError("403 Forbidden"))
    okx = lambda: make_snapshot("okx")
    with patch.object(client, "providers", return_value=[("bybit", blocked), ("okx", okx)]):
        with caplog.at_level(logging.INFO, logger="grambot.derivatives"):
            for attempt in range(1, 4):
                client.backoff_until.clear()  # pretend the pause has elapsed
                assert client.fetch(force=True).provider == "okx"
                assert client.failures["bybit"] == attempt
                assert client.backoff_until["bybit"] - time.time() > FuturesClient.backoff_for(attempt) - 5
        levels = [r.levelno for r in caplog.records if "bybit" in r.getMessage() and "failed" in r.getMessage()]
        assert levels == [logging.WARNING, logging.INFO, logging.INFO]  # warn once, then stay quiet
        assert client.last_error is None  # okx answered, so the chain as a whole is healthy

    client.backoff_until.clear()
    with patch.object(client, "providers", return_value=[("bybit", okx)]):
        client.fetch(force=True)
        assert client.failures["bybit"] == 0  # a success resets the escalation


# -- history and detector -------------------------------------------------------
def test_oi_changes_use_same_provider_history(monitor):
    storage = monitor.storage
    now = time.time()
    storage.add_futures_point("bybit", 40_000_000, 100e6, 0.0001, 2.5, fetched_at=now - 3600)
    storage.add_futures_point("okx", 8_000_000, 20e6, 0.0001, 2.5, fetched_at=now - 3600)
    storage.add_futures_point("bybit", 41_000_000, 102e6, 0.0001, 2.5, fetched_at=now - 240 * 60)
    snap = make_snapshot("bybit", oi=42_000_000, fetched_at=now)
    changes = d.oi_changes(storage, snap, [60, 240, 1440])
    assert changes[60] == pytest.approx(5.0)
    assert changes[240] == pytest.approx(100 / 41, rel=1e-3)
    assert 1440 not in changes  # no history that far back
    assert d.oi_changes(storage, make_snapshot("okx", oi=8_400_000, fetched_at=now), [60]) == {60: pytest.approx(5.0)}
    assert storage.futures_history_span("bybit") == pytest.approx(3 * 3600, abs=1)
    assert storage.futures_history_span("binance") == 0.0


def test_detector_helpers():
    snap = make_snapshot(funding_rate=0.0003)  # 0.03% / 4h = 0.18%/day
    assert d.funding_is_extreme(snap, 0.15)
    assert not d.funding_is_extreme(make_snapshot(funding_rate=0.0001), 0.15)
    assert not d.funding_is_extreme(snap, 0.0)
    snap.oi_changes = {60: 1.0, 240: -7.0, 1440: 12.0}
    hits = d.triggered_oi_windows(snap, [(60, 4.0), (240, 6.0), (1440, 10.0)])
    assert [h[0] for h in hits] == [240, 1440]
    assert d.strongest_oi_window(hits)[0] == 1440  # 12/10 > 7/6
    assert d.strongest_oi_window([]) is None
    assert "шорт" in d.positioning_note(-5.0, 3.0) and "лонг" in d.positioning_note(4.0, 2.0)
    assert "ровной" in d.positioning_note(4.0, 0.1)
    assert "лонгами" in d.funding_note(0.2) and "шортами" in d.funding_note(-0.2)


# -- alerts ---------------------------------------------------------------------
def run_futures_poll(monitor, snapshot, liquidations=None):
    with patch.object(monitor.futures_client, "fetch", return_value=snapshot), patch.object(
        monitor.futures_client, "fetch_liquidations", return_value=liquidations
    ):
        return monitor.poll_futures()


def futures_messages(monitor):
    return [m for m in monitor.notifier.sent if "Деривативы TON" in m]


def test_funding_alert_fires_once_and_rearms_below_half_threshold(monitor):
    monitor.settings.oi_alert_windows = []
    monitor.settings.liquidation_alert_usd = 0.0
    run_futures_poll(monitor, make_snapshot(funding_rate=0.0004))  # 0.24%/day
    assert len(futures_messages(monitor)) == 1
    msg = futures_messages(monitor)[0]
    assert "ставка финансирования +0,24% в день" in msg
    assert "лонги платят шортам" in msg and "не финансовая рекомендация" in msg
    assert "Источник: Bybit" in msg
    assert monitor.storage.get_value(FUTURES_FUNDING_FIRED_KEY) == "1"
    assert monitor.storage.count_signals_since(time.time() - 60) == 1

    run_futures_poll(monitor, make_snapshot(funding_rate=0.0004, fetched_at=time.time() + 1))
    assert len(futures_messages(monitor)) == 1  # still over threshold: no repeat
    run_futures_poll(monitor, make_snapshot(funding_rate=0.0001, fetched_at=time.time() + 2))  # 0.06%/day < 0.075
    assert monitor.storage.get_value(FUTURES_FUNDING_FIRED_KEY) is None
    monitor.storage.set_value(LAST_FUTURES_ALERT_KEY.format(kind="funding"), str(time.time() - 4 * 3600))
    run_futures_poll(monitor, make_snapshot(funding_rate=-0.0004, fetched_at=time.time() + 3))
    assert len(futures_messages(monitor)) == 2
    assert "−0,24% в день" in futures_messages(monitor)[1] and "шорты платят лонгам" in futures_messages(monitor)[1]


def test_funding_alert_respects_cooldown(monitor):
    monitor.settings.oi_alert_windows = []
    monitor.settings.liquidation_alert_usd = 0.0
    monitor.storage.set_value(LAST_FUTURES_ALERT_KEY.format(kind="funding"), str(time.time() - 60))
    run_futures_poll(monitor, make_snapshot(funding_rate=0.0004))
    assert not futures_messages(monitor)
    # Funding alone does not mark the market as active (slow positioning).
    assert monitor.market_active_until() is None


def test_oi_alert_uses_history_and_marks_market_active(monitor):
    monitor.settings.liquidation_alert_usd = 0.0
    now = time.time()
    monitor.storage.add_futures_point("bybit", 40_000_000, 100e6, 0.0001, 2.5, fetched_at=now - 3600)
    snap = make_snapshot(oi=42_000_000, fetched_at=now)  # +5% / 1h over the 4% threshold
    run_futures_poll(monitor, snap)
    msgs = futures_messages(monitor)
    assert len(msgs) == 1
    assert "открытый интерес +5,0% за 1 ч" in msgs[0]
    assert "42,0 млн GRAM" in msgs[0] and "+5,0% за 1 ч" in msgs[0]
    assert "Что это значит" in msgs[0]
    assert monitor.storage.get_value(FUTURES_OI_FIRED_KEY.format(minutes=60)) == "1"
    assert monitor.market_active_until() is not None
    # Stored history point for the snapshot itself (once).
    assert monitor.storage.futures_history_span("bybit") == pytest.approx(3600, abs=1)
    run_futures_poll(monitor, snap)  # cached snapshot: no duplicate row, no duplicate alert
    assert len(futures_messages(monitor)) == 1


def test_liquidation_alert_headline_and_causes(monitor):
    monitor.settings.oi_alert_windows = []
    stats = LiquidationStats("okx", time.time(), 3600, 400_000.0, 350_000.0, 50_000.0, 23, False)
    run_futures_poll(monitor, make_snapshot(), stats)
    msgs = futures_messages(monitor)
    assert len(msgs) == 1
    assert msgs[0].startswith("⚡ Деривативы TON: ликвидации $400 000 за 1 ч (лонги 88%)")
    assert "лонги $350 000, шорты $50 000 (OKX, 23 ордеров)" in msgs[0]
    assert "ликвидируют в основном лонги" in msgs[0]
    assert "Новостей, объясняющих движение" in msgs[0]
    assert monitor.storage.get_value(FUTURES_LIQ_FIRED_KEY) == "1"
    assert monitor.market_active_until() is not None
    # Calm hour re-arms the trigger.
    calm = LiquidationStats("okx", time.time(), 3600, 10_000.0, 10_000.0, 0.0, 2, False)
    run_futures_poll(monitor, make_snapshot(fetched_at=time.time() + 1), calm)
    assert monitor.storage.get_value(FUTURES_LIQ_FIRED_KEY) is None


def test_futures_alert_suppressed_when_muted_or_disabled(monitor):
    monitor.settings.oi_alert_windows = []
    monitor.storage.set_value("muted_until", str(time.time() + 600))
    run_futures_poll(monitor, make_snapshot(funding_rate=0.001))
    assert not futures_messages(monitor)
    monitor.storage.set_value("muted_until", None)
    monitor.futures_enabled = False
    assert run_futures_poll(monitor, make_snapshot(funding_rate=0.001)) is None
    assert not futures_messages(monitor)


def test_futures_poll_reports_health_when_all_providers_fail(monitor):
    monitor.futures_client.last_error = "bybit: 451"
    with patch.object(monitor.futures_client, "fetch", return_value=None):
        assert monitor.poll_futures() is None
    assert dict(monitor.health.summary()).get("futures") not in (None, "ок")


# -- commands and formatting ----------------------------------------------------
def test_futures_command_and_status_line(monitor):
    handler = CommandHandler(monitor, monitor.notifier)
    assert "Деривативы: ожидают первого опроса" in handler.handle_command("/status")
    text = handler.handle_command("/futures")
    assert "Данные пока недоступны" in text and "Алерты:" in text
    assert "финансирование ≥ 0,15%/день" in text and "OI ±4%/1 ч, ±6%/4 ч, ±10%/24 ч" in text
    assert "ликвидации ≥ $250 000/час" in text

    now = time.time()
    monitor.storage.add_futures_point("bybit", 40_000_000, 100e6, 0.0001, 2.5, fetched_at=now - 3600)
    stats = LiquidationStats("okx", now, 3600, 12_000.0, 8_000.0, 4_000.0, 5, False)
    snap = make_snapshot(oi=40_400_000, fetched_at=now)
    with patch.object(monitor.futures_client, "fetch", return_value=snap), patch.object(
        monitor.futures_client, "fetch_liquidations", return_value=stats
    ):
        text = handler.handle_command("/futures")
        status = handler.handle_command("/status")
    assert "Ставка финансирования: +0,010% за 4 ч (≈ +0,06%/день, +22%/год)" in text
    assert "Открытый интерес: 40,4 млн GRAM (≈ $101,0 млн) · +1,0% за 1 ч" in text
    assert "Ликвидации за 1 ч: $12 000 — лонги $8 000, шорты $4 000 (OKX, 5 ордеров)" in text
    assert "История открытого интереса: 1,0 ч из 24 ч" in text
    assert "Деривативы (bybit): финансирование +0,06%/день · OI $101,0 млн · ликвидации $12 000/ч" in status
    assert not futures_messages(monitor)  # /futures never alerts

    monitor.futures_enabled = False
    assert "выключен" in handler.handle_command("/futures")
    assert "Деривативы: выключены" in handler.handle_command("/status")


def test_format_futures_alert_without_optional_data():
    snap = FuturesSnapshot(
        fetched_at=time.time(), provider="okx", symbol="GRAMUSDT", mark_price=None, funding_rate=None,
        funding_interval_hours=8.0, next_funding_at=None, open_interest=None, open_interest_usd=5e6,
        oi_changes={240: 7.5},
    )
    text = format_futures_alert(snap, None, oi_hit=(240, 7.5, 6.0))
    assert "открытый интерес +7,5% за 4 ч" in text and "Открытый интерес: $5,0 млн" in text
    assert "Ставка финансирования" not in text and "Ликвидации" not in text
    assert "Источник: OKX" in text
    status = format_futures_status(None, None, error="bybit: 451")
    assert "bybit: 451" in status
