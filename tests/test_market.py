"""Market move detector v2: multi-window price alerts, volume bursts,
market-confirmed single-source news and "possible cause" headlines."""
import os
import tempfile
import time
from unittest.mock import patch

import pytest
import requests

from grambot import app as app_module
from grambot import market
from grambot import price as price_module
from grambot.commands import CommandHandler
from grambot.config import Settings, parse_windows
from grambot.notifier import (
    fmt_times,
    fmt_window,
    format_impulse_alert,
    format_price_alert,
    format_price_context,
    format_volume_alert,
)
from grambot.storage import Storage

from .conftest import make_item


# -- configuration -------------------------------------------------------------
def test_parse_windows_skips_garbage_and_sorts():
    assert parse_windows("240:6, 60:3.5,bad,10:0,1440:10,60:4") == [(60, 4.0), (240, 6.0), (1440, 10.0)]
    assert parse_windows("") == []


def test_settings_combine_fast_window_with_slow_ones(monkeypatch):
    monkeypatch.setenv("PRICE_ALERT_WINDOWS", "60:3,240:6")
    monkeypatch.setenv("PRICE_WINDOW_MINUTES", "15")
    monkeypatch.setenv("PRICE_ALERT_THRESHOLD_PCT", "4")
    monkeypatch.setenv("VOLUME_SPIKE_RATIO", "1")  # clamped to the minimum
    settings = Settings.from_env()
    assert settings.all_price_windows == [(15, 4.0), (60, 3.0), (240, 6.0)]
    assert settings.price_window_lengths == [15, 60, 240]
    assert settings.volume_spike_ratio == 1.5

    defaults = Settings()
    assert defaults.all_price_windows == [(20, 4.0), (60, 2.5), (240, 5.0), (1440, 8.0)]
    assert defaults.enable_impulse_alerts and (defaults.impulse_fast_pct, defaults.impulse_slow_pct) == (2.0, 1.5)
    assert 60 in Settings(price_alert_windows=[]).price_window_lengths


# -- klines / volume statistics -------------------------------------------------
def make_candles(now, hours=25, base_volume=1000.0, last_hour_volume=None, burst_hour=None):
    candles = []
    total = hours * market.CANDLES_PER_HOUR
    for i in range(total):
        open_time = now - (total - i) * market.CANDLE_MINUTES * 60
        hour_from_end = (total - 1 - i) // market.CANDLES_PER_HOUR
        volume = base_volume
        if hour_from_end == 0 and last_hour_volume is not None:
            volume = last_hour_volume
        if burst_hour is not None and hour_from_end == burst_hour:
            volume = base_volume * 50
        candles.append(market.Candle(open_time=open_time, open=1.0, close=1.0, quote_volume=volume))
    return candles


def test_volume_stats_ratio_uses_median_baseline():
    now = 1_000_000.0
    stats = market.volume_stats(make_candles(now, last_hour_volume=5000.0, burst_hour=5), "binance", now)
    assert stats.volume_1h_usd == pytest.approx(5000.0 * 12)
    assert stats.baseline_hourly_usd == pytest.approx(1000.0 * 12)  # the burst hour does not move the median
    assert stats.ratio == pytest.approx(5.0)
    assert stats.hours_in_baseline == 24


def test_volume_stats_needs_enough_history():
    now = 1_000_000.0
    assert market.volume_stats(make_candles(now, hours=3), "binance", now) is None
    short = market.volume_stats(make_candles(now, hours=8, last_hour_volume=2000.0), "binance", now)
    assert short.ratio == pytest.approx(2.0)
    assert short.hours_in_baseline == 7


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._payload


def _binance_rows(now, n=300):
    rows = []
    for i in range(n):
        ts = int((now - (n - i) * 300) * 1000)
        vol = "5000" if i >= n - 12 else "1000"
        rows.append([ts, "1.6", "1.62", "1.59", "1.61", "3000", ts + 299_999, vol, 10, "1", "1", "0"])
    return rows


def _bybit_payload(now, n=300):
    rows = []
    for i in range(n):  # newest first, turnover at index 6
        ts = str(int((now - (i + 1) * 300) * 1000))
        vol = "4000" if i < 12 else "1000"
        rows.append([ts, "1.6", "1.62", "1.59", "1.61", "3000", vol])
    return {"retCode": 0, "result": {"category": "spot", "symbol": "GRAMUSDT", "list": rows}}


def _okx_payload(now, n=300):
    rows = []
    for i in range(n):  # newest first, volCcy at index 6
        ts = str(int((now - (i + 1) * 300) * 1000))
        vol = "3000" if i < 12 else "1000"
        rows.append([ts, "1.6", "1.62", "1.59", "1.61", "3000", vol, vol, "1"])
    return {"code": "0", "data": rows, "msg": ""}


def test_volume_client_falls_back_when_binance_is_geo_blocked():
    now = time.time()
    responses = {
        market.BINANCE_VISION_KLINES_URL: _Resp({"code": 0, "msg": "Service unavailable from a restricted location"}, 451),
        market.BINANCE_KLINES_URL: _Resp({}, 451),
        market.BYBIT_KLINES_URL: _Resp(_bybit_payload(now)),
        market.OKX_CANDLES_URL: _Resp(_okx_payload(now)),
    }
    client = market.VolumeClient(ttl=300)
    with patch("grambot.price.requests.get", side_effect=lambda url, **kw: responses[url]) as get:
        stats = client.fetch()
        assert stats.provider == "bybit"
        assert stats.ratio == pytest.approx(4.0)
        assert client.backoff_until["binance-vision"] > now
        # Cached: no new request within the TTL.
        assert client.fetch() is stats
        calls_before = get.call_count
        assert client.fetch(force=True).provider == "bybit"
        assert get.call_count > calls_before
        # Blocked providers are not retried while backing off.
        assert all(c.args[0] not in (market.BINANCE_VISION_KLINES_URL, market.BINANCE_KLINES_URL) for c in get.call_args_list[calls_before:])

    responses[market.BYBIT_KLINES_URL] = _Resp({"retCode": 10001, "retMsg": "params error", "result": {}})
    with patch("grambot.price.requests.get", side_effect=lambda url, **kw: responses[url]):
        stats = client.fetch(force=True)
    assert stats.provider == "okx"
    assert stats.ratio == pytest.approx(3.0)


def test_volume_client_parses_binance_and_survives_total_failure():
    now = time.time()
    client = market.VolumeClient()
    with patch("grambot.price.requests.get", return_value=_Resp(_binance_rows(now))):
        stats = client.fetch()
    assert stats.provider == "binance-vision"
    assert stats.ratio == pytest.approx(5.0)

    broken = market.VolumeClient(ttl=60)
    with patch("grambot.price.requests.get", side_effect=requests.ConnectionError("offline")) as get:
        assert broken.fetch() is None
        assert "offline" in broken.last_error
        calls = get.call_count
        assert broken.fetch() is None  # recent failure: no retry storm inside the TTL
        assert get.call_count == calls


# -- multi-window move maths ------------------------------------------------------
@pytest.fixture
def storage():
    with tempfile.TemporaryDirectory() as tmp:
        store = Storage(os.path.join(tmp, "m.db"))
        try:
            yield store
        finally:
            store.close()


def test_compute_move_fills_every_window_and_ignores_stale_points(storage):
    now = 1_000_000.0
    storage.add_price_point(1.50, 1.0, fetched_at=now - 5 * 3600)  # far too old for the 1h/4h windows
    storage.add_price_point(1.60, 1.0, fetched_at=now - 62 * 60)
    storage.add_price_point(1.64, 1.0, fetched_at=now - 21 * 60)
    snap = price_module.PriceSnapshot(price_usd=1.66, volume_24h_usd=1.0, change_24h_pct=None, fetched_at=now)
    move = price_module.compute_move(storage, snap, 20, [60, 240, 1440])
    assert move.window_change_pct == pytest.approx((1.66 - 1.64) / 1.64 * 100)
    assert move.window_changes[60] == pytest.approx((1.66 - 1.60) / 1.60 * 100)
    assert move.window_changes[240] == pytest.approx((1.66 - 1.50) / 1.50 * 100)  # 5h-old point is within the 4h tolerance
    assert move.window_changes[1440] is None
    assert move.change_over(240) == move.window_changes[240]

    hits = market.triggered_windows(move, [(20, 5.0), (60, 3.5), (240, 6.0), (1440, 10.0)])
    assert [h[0] for h in hits] == [60, 240]
    assert market.strongest_window(hits)[0] == 240
    assert market.strongest_window([]) is None


def test_reference_point_prefers_earlier_then_nearest(storage):
    now = 1_000_000.0
    storage.add_price_point(2.0, 1.0, fetched_at=now - 45 * 60)  # only point: 45 min old
    snap = price_module.PriceSnapshot(price_usd=2.1, volume_24h_usd=1.0, change_24h_pct=None, fetched_at=now)
    move = price_module.compute_move(storage, snap, 20, [60, 240])
    assert move.window_changes[20] is None  # 45 min is far outside the 20-min tolerance
    assert move.window_changes[60] == pytest.approx(5.0)  # nearest point after "1h ago" within tolerance
    assert move.window_changes[240] is None


def test_is_volume_spike_requires_ratio_and_move():
    move = price_module.PriceMove(1.0, 20, None, None, 0.0, None, window_changes={60: 2.5}, volume_1h_ratio=4.5)
    assert market.is_volume_spike(move, 4.0, 2.0)
    assert not market.is_volume_spike(move, 5.0, 2.0)
    move.window_changes[60] = 1.0
    assert not market.is_volume_spike(move, 4.0, 2.0)
    assert market.is_volume_spike(move, 4.0, 0.0)
    move.window_changes[60] = None
    assert not market.is_volume_spike(move, 4.0, 2.0)
    move.volume_1h_ratio = None
    assert not market.is_volume_spike(move, 4.0, 0.0)


# -- formatting ----------------------------------------------------------------------
def test_formatting_helpers_for_windows_and_multipliers():
    assert fmt_window(20) == "20 мин" and fmt_window(60) == "1 ч" and fmt_window(240) == "4 ч"
    assert fmt_window(1440) == "24 ч" and fmt_window(2880) == "2 д" and fmt_window(90) == "1,5 ч"
    assert fmt_times(4.2) == "в 4,2 раза" and fmt_times(3.0) == "в 3 раза" and fmt_times(5.02) == "в 5 раз"
    assert fmt_times(22.0) == "в 22 раза" and fmt_times(12.0) == "в 12 раз"


def test_price_context_lists_windows_and_hourly_volume():
    move = price_module.PriceMove(
        1.63, 20, 1.2, 10.5, 63_100_000.0, 1.4,
        window_changes={20: 1.2, 60: 3.6, 240: 6.1, 1440: 10.9},
        volume_1h_usd=3_500_000.0, volume_1h_ratio=4.2, volume_provider="binance",
    )
    lines = format_price_context(move)
    assert lines[0] == "TON: $1,630 · +10,5% за 24ч"
    assert lines[1] == "+1,2% за 20 мин · +3,6% за 1 ч · +6,1% за 4 ч"  # 24h shown once, on the first line
    assert lines[2] == "Объём 24ч: $63,1 млн (×1,4 к вчера)"
    assert lines[3] == "Объём за час: $3,5 млн — в 4,2 раза выше обычного"

    move.change_24h_pct = None
    move.volume_1h_ratio = 0.8
    lines = format_price_context(move)
    assert lines[0] == "TON: $1,630 · +10,9% за 24ч"  # falls back to the stored 24h window
    assert lines[3] == "Объём за час: $3,5 млн (×0,8 к обычному)"


def test_price_and_volume_alert_texts_with_causes():
    move = price_module.PriceMove(1.63, 20, 1.2, 10.5, 1.0, None, window_changes={20: 1.2, 60: 3.6, 1440: 10.5}, volume_1h_ratio=4.0)
    cause = make_item("Verb announces $558M TON treasury", source="CoinMarketCap", minutes_ago=30, url="https://cmc.example/a?x=1&y=2")
    text = format_price_alert(move, 1440, [cause])
    assert text.startswith("📈 Рост TON: +10,5% за 24 ч")
    assert "Возможная причина (не подтверждено):" in text
    assert '• <a href="https://cmc.example/a?x=1&amp;y=2">Verb announces $558M TON treasury</a> — CoinMarketCap' in text
    assert "не финансовая рекомендация" in text

    assert format_price_alert(move, 60).startswith("📈 Рост TON: +3,6% за 1 ч")
    assert format_price_alert(move).startswith("📈 Резкий рост TON: +1,2% за 20 мин")
    move.window_changes[60] = -4.4
    volume = format_volume_alert(move)
    assert volume.startswith("📊 Всплеск объёма TON: в 4 раза выше обычного за час (−4,4% за 1 ч)")
    assert "Новостей, объясняющих движение, пока не найдено" in volume


# -- detector behaviour in the monitor ---------------------------------------------
def snapshot(price, now, change_24h=None):
    return price_module.PriceSnapshot(price_usd=price, volume_24h_usd=1_000.0, change_24h_pct=change_24h, fetched_at=now)


def test_gradual_move_triggers_slow_window_and_activates_market(monitor):
    now = time.time()
    monitor.storage.add_price_point(2.00, 1000.0, fetched_at=now - 65 * 60)
    monitor.storage.add_price_point(2.05, 1000.0, fetched_at=now - 25 * 60)
    with patch.object(price_module.PriceClient, "fetch", return_value=snapshot(2.08, now)):
        move = monitor.poll_price()
    assert move.window_change_pct == pytest.approx((2.08 - 2.05) / 2.05 * 100)  # +1.5% in 20 min: old rule stays silent
    assert move.window_changes[60] == pytest.approx(4.0)
    assert len(monitor.notifier.sent) == 1
    assert monitor.notifier.sent[0].startswith("📈 Рост TON: +4,0% за 1 ч")
    assert "+1,5% за 20 мин · +4,0% за 1 ч" in monitor.notifier.sent[0]
    assert monitor.market_active_until() > now
    signal = monitor.storage.recent_signals(1)[0]
    assert signal.kind == "price" and "за 60 мин" in signal.title


def test_window_alert_fires_once_until_it_rearms(monitor):
    now = time.time()
    monitor.storage.add_price_point(2.00, 1000.0, fetched_at=now - 65 * 60)
    with patch.object(price_module.PriceClient, "fetch", return_value=snapshot(2.08, now)):
        monitor.poll_price()
    assert len(monitor.notifier.sent) == 1

    monitor.storage.set_value(app_module.LAST_PRICE_ALERT_KEY, str(now - 7200))  # cooldown long over
    with patch.object(price_module.PriceClient, "fetch", return_value=snapshot(2.085, now + 120)):
        monitor.poll_price()
    assert len(monitor.notifier.sent) == 1  # still +4% over 1h: the window has fired already

    # The move fades below half the threshold: the window re-arms...
    with patch.object(price_module.PriceClient, "fetch", return_value=snapshot(2.02, now + 240)):
        monitor.poll_price()
    assert len(monitor.notifier.sent) == 1
    # ...and a fresh leg alerts again.
    with patch.object(price_module.PriceClient, "fetch", return_value=snapshot(2.09, now + 360)):
        monitor.poll_price()
    assert len(monitor.notifier.sent) == 2


def test_price_alert_lists_recent_headlines_as_possible_cause(monitor):
    now = time.time()
    item = make_item("Verb announces $558M TON treasury", source="CoinMarketCap", minutes_ago=40)
    monitor.source.queue([item])
    assert monitor.poll_news_once() == 0  # single source: not verified yet
    monitor.storage.add_price_point(2.00, 1000.0, fetched_at=now - 65 * 60)
    with patch.object(price_module.PriceClient, "fetch", return_value=snapshot(2.08, now)):
        monitor.poll_price()
    text = monitor.notifier.sent[-1]
    assert "Возможная причина (не подтверждено):" in text
    assert "Verb announces $558M TON treasury" in text and "CoinMarketCap" in text
    assert monitor.storage.recent_signals(1)[0].source_count == 1


def test_market_move_confirms_single_source_news_with_limit(monitor):
    monitor.settings.market_confirmed_news_limit = 1
    now = time.time()
    monitor.storage.add_price_point(2.00, 1000.0, fetched_at=now - 65 * 60)
    with patch.object(price_module.PriceClient, "fetch", return_value=snapshot(2.08, now)):
        monitor.poll_price()
    assert len(monitor.notifier.sent) == 1

    monitor.source.queue([make_item("Binance delists TON", source="Only Source", minutes_ago=10)])
    assert monitor.poll_news_once() == 1
    assert "Источники: 1 (Only Source) · совпадает с движением рынка" in monitor.notifier.sent[-1]
    assert "не подтверждено" not in monitor.notifier.sent[-1]

    # Limit reached for this active period: the next lone headline waits for a second source.
    monitor.source.queue([make_item("Coinbase halts Toncoin trading", source="Another", minutes_ago=5)])
    assert monitor.poll_news_once() == 0
    # An old headline is never "confirmed" by today's move either.
    monitor.settings.market_confirmed_news_limit = 5
    monitor.source.queue([make_item("Kraken suspends TON deposits", source="Old", minutes_ago=6 * 60)])
    assert monitor.poll_news_once() == 0


def test_market_confirmation_is_off_without_a_move_or_when_disabled(monitor):
    monitor.source.queue([make_item("Binance delists TON", source="Only Source")])
    assert monitor.poll_news_once() == 0
    assert monitor.market_active_until() is None

    monitor.settings.market_confirms_news = False
    now = time.time()
    monitor.storage.add_price_point(2.00, 1000.0, fetched_at=now - 65 * 60)
    with patch.object(price_module.PriceClient, "fetch", return_value=snapshot(2.08, now)):
        monitor.poll_price()
    monitor.source.queue([make_item("Coinbase halts Toncoin trading", source="Another")])
    assert monitor.poll_news_once() == 0


def test_volume_burst_alerts_separately_with_its_own_cooldown(monitor):
    now = time.time()
    monitor.storage.add_price_point(2.00, 1000.0, fetched_at=now - 65 * 60)
    stats = market.VolumeStats(volume_1h_usd=3_500_000.0, baseline_hourly_usd=700_000.0, ratio=5.0, provider="bybit", fetched_at=now, hours_in_baseline=24)
    with patch.object(market.VolumeClient, "fetch", return_value=stats), patch.object(
        price_module.PriceClient, "fetch", return_value=snapshot(2.05, now)  # +2.5% in 1h: below every price threshold
    ):
        move = monitor.poll_price()
        assert move.volume_1h_ratio == 5.0 and move.volume_provider == "bybit"
        assert len(monitor.notifier.sent) == 1
        assert monitor.notifier.sent[0].startswith("📊 Всплеск объёма TON: в 5 раз выше обычного за час (+2,5% за 1 ч)")
        assert "Объём за час: $3,5 млн — в 5 раз выше обычного" in monitor.notifier.sent[0]
        assert monitor.market_active_until() > now
        assert monitor.storage.recent_signals(1)[0].kind == "volume"
        monitor.poll_price()
        assert len(monitor.notifier.sent) == 1  # fired + cooldown

    # A price alert in the same poll absorbs the volume information instead of sending two messages.
    monitor.storage.set_value(app_module.VOLUME_FIRED_KEY, None)
    monitor.storage.set_value(app_module.LAST_VOLUME_ALERT_KEY, None)
    with patch.object(market.VolumeClient, "fetch", return_value=stats), patch.object(
        price_module.PriceClient, "fetch", return_value=snapshot(2.12, now + 120)
    ):
        monitor.poll_price()
    assert len(monitor.notifier.sent) == 2
    assert monitor.notifier.sent[1].startswith("📈 Рост TON: +6,0% за 1 ч")
    assert "в 5 раз выше обычного" in monitor.notifier.sent[1]
    assert monitor.storage.get_value(app_module.VOLUME_FIRED_KEY) == "1"

    monitor.settings.enable_volume_alerts = False
    with patch.object(market.VolumeClient, "fetch", return_value=stats) as fetch, patch.object(
        price_module.PriceClient, "fetch", return_value=snapshot(2.12, now + 240)
    ):
        move = monitor.poll_price()
    fetch.assert_not_called()
    assert move.volume_1h_ratio is None


def test_price_and_status_commands_show_market_details(monitor):
    handler = CommandHandler(monitor, monitor.notifier)
    now = time.time()
    monitor.storage.add_price_point(2.00, 1000.0, fetched_at=now - 65 * 60)
    stats = market.VolumeStats(volume_1h_usd=2_000_000.0, baseline_hourly_usd=1_000_000.0, ratio=2.0, provider="okx", fetched_at=now, hours_in_baseline=24)
    monitor.volume_client.last_provider = "okx"
    with patch.object(market.VolumeClient, "fetch", return_value=stats), patch.object(
        price_module.PriceClient, "fetch", return_value=snapshot(2.08, now, change_24h=9.0)
    ):
        text = handler.handle_command("/price")
    assert "+4,0% за 1 ч" in text
    assert "Объём за час: $2,0 млн — в 2 раза выше обычного" in text
    assert "объём okx" in text
    assert "Рынок в движении" in text

    monitor.volume_client.last_stats = stats
    status = handler.handle_command("/status")
    assert "Алерты цены: 5,0%/20 мин · 2,5%/1 ч · 5,0%/4 ч · 8,0%/24 ч; импульс 2,0%/20 мин при 1,5%/1 ч; объём ×4 (okx)" in status

    monitor.volume_client.last_stats = None
    monitor.volume_client.last_error = "bybit: HTTP 451"
    assert "объём недоступен — bybit: HTTP 451" in handler.handle_command("/status")
    monitor.settings.enable_volume_alerts = False
    assert "объём выключен" in handler.handle_command("/status")


# -- early warning (impulse) ---------------------------------------------------------
def test_impulse_rule_needs_fast_move_confirmed_by_the_hour():
    move = price_module.PriceMove(1.0, 20, None, None, 0.0, None, window_changes={20: 2.1, 60: 1.9})
    assert market.impulse(move, 20, 2.0, 1.5) == (2.1, 1.9)
    move.window_changes[60] = 1.2  # hour has not confirmed yet
    assert market.impulse(move, 20, 2.0, 1.5) is None
    assert market.impulse(move, 20, 2.0, 0.0) == (2.1, 1.2)  # confirmation disabled
    move.window_changes[60] = -1.8  # a bounce inside a falling hour is not an impulse
    assert market.impulse(move, 20, 2.0, 1.5) is None
    move.window_changes.update({20: -2.4, 60: -1.8})
    assert market.impulse(move, 20, 2.0, 1.5) == (-2.4, -1.8)
    move.window_changes[20] = None
    assert market.impulse(move, 20, 2.0, 1.5) is None


def test_impulse_alert_text():
    move = price_module.PriceMove(1.614, 20, 1.9, 9.8, 1.0, None, window_changes={20: 1.9, 60: 1.6, 240: -0.5, 1440: 9.8})
    text = format_impulse_alert(move, 20)
    assert text.startswith("⚡ Импульс TON: +1,9% за 20 мин")
    assert "+1,9% за 20 мин · +1,6% за 1 ч · −0,5% за 4 ч" in text
    assert "Раннее предупреждение" in text and "Если рост продолжится, придёт обычный сигнал." in text
    assert "не финансовая рекомендация" in text
    move.window_changes[20] = -2.2
    assert "Если падение продолжится" in format_impulse_alert(move, 20)


def test_impulse_warns_early_and_the_regular_alert_still_follows(monitor):
    """Replay of 27.09: +2.1%/20 min at 15:20 UTC, +3.3%/1 h at 15:45."""
    t0 = time.time() - 3600
    monitor.storage.add_price_point(1.590, 1000.0, fetched_at=t0 - 40 * 60)  # 14:20
    monitor.storage.add_price_point(1.588, 1000.0, fetched_at=t0)  # 15:00
    monitor.storage.add_price_point(1.587, 1000.0, fetched_at=t0 + 5 * 60)
    monitor.storage.add_price_point(1.608, 1000.0, fetched_at=t0 + 10 * 60)
    monitor.storage.add_price_point(1.614, 1000.0, fetched_at=t0 + 15 * 60)
    with patch.object(price_module.PriceClient, "fetch", return_value=snapshot(1.621, t0 + 20 * 60)):
        monitor.poll_price()  # 15:20 — +2.1% in 20 min, +2.1% over the hour
    assert len(monitor.notifier.sent) == 1
    assert monitor.notifier.sent[0].startswith("⚡ Импульс TON: +2,1% за 20 мин")
    assert monitor.market_active_until() is None  # weak evidence: does not confirm single-source news
    signal = monitor.storage.recent_signals(1)[0]
    assert signal.kind == "impulse" and signal.sentiment == "positive" and signal.strength == "low"

    monitor.storage.add_price_point(1.608, 1000.0, fetched_at=t0 + 25 * 60)
    with patch.object(price_module.PriceClient, "fetch", return_value=snapshot(1.624, t0 + 35 * 60)):
        monitor.poll_price()  # +0.6% in 20 min: the impulse rule re-arms and stays quiet
    assert len(monitor.notifier.sent) == 1

    with patch.object(price_module.PriceClient, "fetch", return_value=snapshot(1.640, t0 + 45 * 60)):
        monitor.poll_price()  # 15:45 — +3.3% over the hour crosses the 2.5% window
    assert len(monitor.notifier.sent) == 2
    assert monitor.notifier.sent[1].startswith("📈 Рост TON: +3,3% за 1 ч")  # the impulse did not eat the cooldown
    assert monitor.market_active_until() > t0

    # Another +2% leg right after the regular alert adds nothing new.
    monitor.storage.set_value(app_module.IMPULSE_FIRED_KEY, None)
    with patch.object(price_module.PriceClient, "fetch", return_value=snapshot(1.674, t0 + 60 * 60)):
        monitor.poll_price()  # +3.0%/20 min, +5.4%/1 h: window fired, impulse inside the price-alert cooldown
    assert len(monitor.notifier.sent) == 2


def test_impulse_cooldown_rearm_and_switch(monitor):
    now = time.time()
    monitor.storage.add_price_point(2.00, 1000.0, fetched_at=now - 60 * 60)
    monitor.storage.add_price_point(2.00, 1000.0, fetched_at=now - 20 * 60)
    with patch.object(price_module.PriceClient, "fetch", return_value=snapshot(2.045, now)):
        monitor.poll_price()
    assert len(monitor.notifier.sent) == 1 and monitor.notifier.sent[0].startswith("⚡")

    monitor.storage.set_value(app_module.LAST_IMPULSE_ALERT_KEY, str(now - 7200))  # cooldown over, still fired
    with patch.object(price_module.PriceClient, "fetch", return_value=snapshot(2.046, now + 120)):
        monitor.poll_price()
    assert len(monitor.notifier.sent) == 1
    monitor.storage.add_price_point(2.046, 1000.0, fetched_at=now + 120)
    with patch.object(price_module.PriceClient, "fetch", return_value=snapshot(2.047, now + 22 * 60)):
        monitor.poll_price()  # +0.05% in 20 min: re-arms
    assert monitor.storage.get_value(app_module.IMPULSE_FIRED_KEY) is None
    with patch.object(price_module.PriceClient, "fetch", return_value=snapshot(2.09, now + 24 * 60)):
        monitor.poll_price()  # fresh +2.1% leg, hour +4.5%: the regular alert wins, no second impulse
    assert len(monitor.notifier.sent) == 2 and monitor.notifier.sent[1].startswith("📈 Рост TON")

    monitor.settings.enable_impulse_alerts = False
    monitor.storage.set_value(app_module.LAST_PRICE_ALERT_KEY, str(now - 7200))
    for minutes in (20, 60, 240, 1440):
        monitor.storage.set_value(app_module.PRICE_WINDOW_FIRED_KEY.format(minutes=minutes), None)
    monitor.storage.add_price_point(2.00, 1000.0, fetched_at=now + 40 * 60)
    monitor.storage.add_price_point(2.00, 1000.0, fetched_at=now + 80 * 60)
    with patch.object(price_module.PriceClient, "fetch", return_value=snapshot(2.045, now + 100 * 60)):
        monitor.poll_price()
    assert len(monitor.notifier.sent) == 2  # disabled: +2.25%/20 min alone is not a price alert


def test_stats_count_impulses_and_their_follow_through(monitor):
    now = time.time()
    monitor.storage.record_signal("impulse", "Импульс TON +2.10%", None, "positive", "low", 0, 1.60, sent_at=now - 7200)
    monitor.storage.record_signal("impulse", "Импульс TON -2.30%", None, "negative", "low", 0, 1.60, sent_at=now - 7000)
    monitor.storage.record_signal("impulse", "Импульс TON +2.00%", None, "positive", "low", 0, 1.60, sent_at=now - 60)
    monitor.storage.add_price_point(1.65, 1000.0, fetched_at=now - 3600)  # an hour after the first two
    assert monitor.update_signal_followups() == 2
    stats = monitor.storage.signal_stats()
    assert (stats.total_impulse, stats.impulse_evaluated, stats.impulse_continued) == (3, 2, 1)
    text = CommandHandler(monitor, monitor.notifier).handle_command("/stats")
    assert "Импульсов (ранних предупреждений): 3, продолжились через 1ч: 1 из 2" in text
