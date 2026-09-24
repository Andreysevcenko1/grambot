import json
import time
from unittest.mock import patch

import pytest

from grambot import onchain
from grambot.app import LAST_WHALE_ALERT_KEY, NETWORK_STALLED_SINCE_KEY, ONCHAIN_LAST_UTIME_KEY
from grambot.notifier import format_network_alert, format_whale_alert
from grambot.onchain import (
    Label,
    Labels,
    MasterchainState,
    RateLimited,
    TonCenterClient,
    Transfer,
    classify_transfer,
    message_to_transfer,
    parse_labels_dataset,
    raw_to_friendly,
    scan_transfers,
    transfer_sentiment,
)

ZERO = "0:0000000000000000000000000000000000000000000000000000000000000000"
BINANCE = "0:00000000000000000000000000000000000000000000000000000000000000B1"
OKX = "0:00000000000000000000000000000000000000000000000000000000000000C2"
FUND = "0:00000000000000000000000000000000000000000000000000000000000000F1"
POOL = "0:00000000000000000000000000000000000000000000000000000000000000E1"
WALLET_A = "0:00000000000000000000000000000000000000000000000000000000000000A1"
WALLET_B = "0:00000000000000000000000000000000000000000000000000000000000000A2"
ELECTOR = "-1:3333333333333333333333333333333333333333333333333333333333333333"


@pytest.fixture
def labels(tmp_path):
    path = tmp_path / "labels.json"
    path.write_text(
        json.dumps(
            {
                "addresses": {
                    BINANCE: ["Binance", "CEX", "hot wallet"],
                    OKX.lower(): ["OKX", "CEX", ""],
                    FUND: ["TON Foundation", "fund", ""],
                    POOL: ["Tonstakers", "liquid-staking", ""],
                }
            }
        )
    )
    return Labels(path=path, url=None)


class _Resp:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise onchain.requests.HTTPError(f"status {self.status_code}")


def _msg(source, destination, ton, created, hash_=None, bounced=False):
    return {
        "hash": hash_ or f"{source[-2:]}{destination[-2:]}{created}",
        "source": source,
        "destination": destination,
        "value": str(int(ton * onchain.NANO)),
        "created_at": str(created),
        "bounced": bounced,
    }


# -- pure helpers ----------------------------------------------------------
def test_raw_to_friendly_matches_known_address():
    assert raw_to_friendly(ZERO) == "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c"
    assert raw_to_friendly(ZERO, bounceable=False).startswith("UQ")


def test_labels_lookup_is_case_insensitive(labels):
    assert labels.get(BINANCE.lower()).name == "Binance"
    assert labels.get(OKX).name == "OKX"
    assert labels.get(WALLET_A) is None
    assert len(labels) == 4


def test_parse_labels_dataset_json_lines_and_categories():
    lines = "\n".join(
        [
            json.dumps({"address": BINANCE, "name": "Binance", "category": "CEX", "comment": "x"}),
            json.dumps({"address": WALLET_A, "name": "Game", "category": "gaming"}),
            json.dumps({"address": "", "name": "Broken", "category": "CEX"}),
        ]
    )
    parsed = parse_labels_dataset(lines)
    assert set(parsed) == {BINANCE}
    assert parsed[BINANCE].display() == "Binance (x)"
    assert parse_labels_dataset(json.dumps([{"address": OKX, "label": "OKX", "category": "CEX"}]))[OKX].name == "OKX"


def test_labels_refresh_ignores_bad_or_shrunken_data(labels):
    labels.url = "https://labels.example/assets.json"
    with patch.object(onchain.requests, "get", return_value=_Resp(None, 500)):
        assert labels.refresh() == 0
    assert labels.source == "bundled"
    tiny = _Resp(None)
    tiny.text = json.dumps({"address": BINANCE, "name": "B", "category": "CEX"})
    with patch.object(onchain.requests, "get", return_value=tiny):
        assert labels.refresh() == 0  # 1 << 4 entries: rejected
    full = _Resp(None)
    full.text = "\n".join(json.dumps({"address": a, "name": "X", "category": "CEX"}) for a in (BINANCE, OKX, FUND, POOL, WALLET_A))
    with patch.object(onchain.requests, "get", return_value=full):
        assert labels.refresh() == 5
    assert labels.source == "remote"
    assert labels.refresh_if_stale() is False
    labels.url = None
    assert labels.refresh() == 0


@pytest.mark.parametrize(
    "src,dst,kind",
    [
        (None, Label("Binance", "CEX"), "exchange_deposit"),
        (Label("Binance", "CEX"), None, "exchange_withdrawal"),
        (Label("Binance", "CEX"), Label("OKX", "CEX"), "exchange_to_exchange"),
        (Label("Binance", "CEX", "hot"), Label("binance", "CEX", "cold"), "exchange_internal"),
        (Label("Tonstakers", "liquid-staking"), Label("Binance", "CEX"), "staking"),
        (Label("Fund", "fund"), None, "fund"),
        (None, Label("STON.fi", "DEX"), "dex"),
        (Label("Bridge", "bridge"), None, "bridge"),
        (None, None, "unknown"),
    ],
)
def test_classify_transfer(src, dst, kind):
    assert classify_transfer(src, dst) == kind


def test_transfer_sentiment_and_strength():
    def t(kind, amount):
        return Transfer("h", 0, amount, WALLET_A, BINANCE, None, None, kind)

    assert transfer_sentiment(t("exchange_deposit", 500_000), 500_000) == ("negative", "low")
    assert transfer_sentiment(t("exchange_withdrawal", 1_000_000), 500_000) == ("positive", "medium")
    assert transfer_sentiment(t("unknown", 2_000_000), 500_000) == ("unknown", "high")


def test_message_to_transfer_filters(labels):
    min_nano = 100 * onchain.NANO
    book = {WALLET_A: {"user_friendly": "EQ_A", "domain": None}}
    tr = message_to_transfer(_msg(WALLET_A, BINANCE, 150, 1000, "h1"), labels, book, min_nano)
    assert tr is not None
    assert tr.kind == "exchange_deposit"
    assert tr.amount_ton == 150
    assert tr.source_friendly == "EQ_A"
    assert tr.destination_friendly == raw_to_friendly(BINANCE)
    assert tr.url == "https://tonviewer.com/transaction/h1"
    assert message_to_transfer(_msg(WALLET_A, BINANCE, 99, 1000), labels, book, min_nano) is None
    assert message_to_transfer(_msg(WALLET_A, BINANCE, 150, 1000, bounced=True), labels, book, min_nano) is None
    assert message_to_transfer(_msg(ELECTOR, WALLET_A, 150, 1000), labels, book, min_nano) is None
    assert message_to_transfer({"source": None, "destination": WALLET_A, "value": "1"}, labels, book, 0) is None
    assert message_to_transfer(_msg(WALLET_A, WALLET_B, 150, 1000) | {"value": "abc"}, labels, book, 0) is None


# -- client ----------------------------------------------------------------
def test_client_rate_limit_and_headers():
    client = TonCenterClient(api_key="key", min_spacing=0)
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append((url, params, headers))
        return _Resp({"messages": [], "address_book": {}})

    with patch.object(client._session, "get", side_effect=fake_get):
        rows, book = client.messages(10, 20, 0)
    assert rows == [] and book == {}
    url, params, headers = calls[0]
    assert url.endswith("/messages")
    assert params["start_utime"] == 10 and params["end_utime"] == 20 and params["sort"] == "asc"
    assert headers["X-API-Key"] == "key"

    with patch.object(client._session, "get", return_value=_Resp({}, 429, {"Retry-After": "7"})):
        with pytest.raises(RateLimited) as exc:
            client.masterchain_state()
    assert exc.value.retry_after == 7

    with patch.object(client._session, "get", return_value=_Resp({"last": {"seqno": "5", "gen_utime": "100"}})):
        state = client.masterchain_state()
    assert state.seqno == 5 and state.gen_utime == 100.0


def test_client_spaces_requests_without_key():
    client = TonCenterClient(min_spacing=0.05)
    with patch.object(client._session, "get", return_value=_Resp({"last": {"seqno": 1, "gen_utime": 1}})):
        started = time.time()
        client.masterchain_state()
        client.masterchain_state()
    assert time.time() - started >= 0.05


# -- scanning --------------------------------------------------------------
class FakeClient:
    def __init__(self, pages, error=None):
        self.pages = pages
        self.error = error
        self.calls = []
        self.backoff_until = 0.0
        self.last_error = None

    def messages(self, start_utime, end_utime, offset, limit=onchain.PAGE_SIZE):
        self.calls.append((start_utime, end_utime, offset))
        if self.error:
            raise self.error
        index = offset // onchain.PAGE_SIZE
        rows = self.pages[index] if index < len(self.pages) else []
        return rows, {}


def test_scan_transfers_collects_and_advances(labels):
    now = 10_000
    rows = [
        _msg(WALLET_A, BINANCE, 600, 9_950, "big"),
        _msg(WALLET_A, WALLET_B, 10, 9_960, "small"),
        _msg(BINANCE, WALLET_B, 700, 9_970, "out"),
        _msg(WALLET_A, BINANCE, 600, 9_950, "big"),  # duplicate row
    ]
    client = FakeClient([rows])
    result = scan_transfers(client, labels, since_utime=9_900, min_ton=100, max_pages=3, now=now)
    assert result.complete and result.error is None
    assert [t.hash for t in result.transfers] == ["big", "out"]
    assert result.transfers[0].kind == "exchange_deposit"
    assert result.transfers[1].kind == "exchange_withdrawal"
    assert result.last_utime == now - 5  # short page: caught up to the end of the window
    assert client.calls == [(9_901, now - 5, 0)]


def test_scan_transfers_page_cap_and_gap(labels):
    now = 10_000
    full_page = [_msg(WALLET_A, WALLET_B, 1, 9_000 + i // 10, f"m{i}") for i in range(onchain.PAGE_SIZE)]
    client = FakeClient([full_page, full_page])
    result = scan_transfers(client, labels, since_utime=8_000, min_ton=100, max_pages=1, now=now)
    assert not result.complete
    assert result.pages == 1 and result.messages == onchain.PAGE_SIZE
    assert result.last_utime == 9_099 - 1  # resumes one second before the last message seen
    assert result.gap_seconds == pytest.approx(200)  # 2000s behind, only 1800 allowed
    assert client.calls[0][0] == now - onchain.MAX_LAG_SECONDS + 1


def test_scan_transfers_errors_set_backoff(labels):
    client = FakeClient([], error=RateLimited(30))
    result = scan_transfers(client, labels, since_utime=9_990, min_ton=100, now=10_000)
    assert not result.complete and "rate limited" in result.error
    assert client.backoff_until > time.time() + 20

    client = FakeClient([], error=onchain.requests.ConnectionError("down"))
    result = scan_transfers(client, labels, since_utime=9_990, min_ton=100, now=10_000)
    assert not result.complete and result.error == "down"
    assert result.last_utime == 9_990  # nothing skipped

    assert scan_transfers(FakeClient([]), labels, since_utime=9_999, min_ton=1, now=10_000).pages == 0


# -- monitor integration -----------------------------------------------------
def _install_chain(monitor, labels, pages, gen_utime=None):
    monitor.labels = labels
    client = FakeClient(pages)
    state = MasterchainState(seqno=42, gen_utime=gen_utime if gen_utime is not None else time.time(), fetched_at=time.time())
    client.masterchain_state = lambda: state
    monitor.ton_client = client
    return client


def test_poll_onchain_records_and_alerts(monitor, labels):
    now = time.time()
    monitor.storage.set_value(ONCHAIN_LAST_UTIME_KEY, str(now - 100))
    rows = [
        _msg(WALLET_A, BINANCE, 600_000, int(now - 50), "dep"),
        _msg(WALLET_A, WALLET_B, 60_000, int(now - 40), "stat"),  # stats only (>= min/10)
        _msg(POOL, WALLET_B, 900_000, int(now - 30), "stake"),  # staking never alerts
        _msg(BINANCE, BINANCE, 900_000, int(now - 20), "internal"),
    ]
    _install_chain(monitor, labels, [rows])
    result = monitor.poll_onchain()
    assert result is not None and result.complete
    assert len(monitor.notifier.sent) == 1
    text = monitor.notifier.sent[0]
    assert "🐋" in text and "Binance" in text and "600 тыс. TON" in text and "депозит на биржу" in text
    assert "tonviewer.com/transaction/dep" in text
    assert monitor.storage.get_float(ONCHAIN_LAST_UTIME_KEY) > now - 100
    assert monitor.storage.get_float(LAST_WHALE_ALERT_KEY) is not None
    signals = monitor.storage.recent_signals()
    assert signals[0].kind == "onchain" and signals[0].sentiment == "negative"

    flows = monitor.storage.flow_stats(now - 3600)
    assert flows.total_count == 4
    assert flows.deposits_ton == 600_000 and flows.deposits_count == 1
    recorded = monitor.storage.recent_transfers(now - 3600, limit=10)
    assert {t.hash for t in recorded} == {"dep", "stat", "stake", "internal"}
    assert next(t for t in recorded if t.hash == "dep").notified

    # Same rows again: nothing new is recorded or sent.
    monitor.poll_onchain()
    assert len(monitor.notifier.sent) == 1
    assert monitor.storage.flow_stats(now - 3600).total_count == 4


def test_whale_cooldown_bypassed_for_huge_transfers(monitor, labels):
    now = time.time()
    monitor.storage.set_value(ONCHAIN_LAST_UTIME_KEY, str(now - 100))
    rows = [
        _msg(BINANCE, WALLET_A, 500_000, int(now - 50), "w1"),
        _msg(BINANCE, WALLET_A, 600_000, int(now - 40), "w2"),  # cooldown
        _msg(BINANCE, WALLET_A, 1_200_000, int(now - 30), "w3"),  # 2× threshold bypasses cooldown
    ]
    _install_chain(monitor, labels, [rows])
    monitor.poll_onchain()
    assert len(monitor.notifier.sent) == 2
    assert "вывод с биржи" in monitor.notifier.sent[0]
    assert "1,20 млн TON" in monitor.notifier.sent[1]


def test_poll_onchain_disabled_and_backoff(monitor, labels):
    monitor.onchain_enabled = False
    assert monitor.poll_onchain() is None
    monitor.onchain_enabled = True
    client = _install_chain(monitor, labels, [[]])
    client.backoff_until = time.time() + 100
    assert monitor.poll_onchain() is None
    assert client.calls == []
    status = monitor.onchain_status()
    assert status["backoff_seconds"] > 0 and status["enabled"]


def test_network_stall_alert_and_recovery(monitor, labels):
    now = time.time()
    monitor.storage.set_value(ONCHAIN_LAST_UTIME_KEY, str(now - 10))
    client = _install_chain(monitor, labels, [[]], gen_utime=now - 10 * 60)
    monitor.poll_onchain()
    assert monitor.storage.get_float(NETWORK_STALLED_SINCE_KEY) is not None
    assert len(monitor.notifier.sent) == 1 and "остановка сети" in monitor.notifier.sent[0].lower()
    assert monitor.storage.recent_signals()[0].strength == "high"

    monitor.poll_onchain()  # still stalled: no repeat
    assert len(monitor.notifier.sent) == 1

    client.masterchain_state = lambda: MasterchainState(seqno=43, gen_utime=time.time(), fetched_at=time.time())
    monitor.poll_onchain()
    assert monitor.storage.get_float(NETWORK_STALLED_SINCE_KEY) is None
    assert len(monitor.notifier.sent) == 2 and "снова" in monitor.notifier.sent[1]


def test_network_api_error_is_not_a_stall(monitor, labels):
    now = time.time()
    monitor.storage.set_value(ONCHAIN_LAST_UTIME_KEY, str(now - 10))
    client = _install_chain(monitor, labels, [[]])

    def boom():
        raise onchain.requests.ConnectionError("down")

    client.masterchain_state = boom
    monitor.poll_onchain()
    assert monitor.notifier.sent == []
    assert monitor.onchain_status()["error"] == "down"


def test_run_once_includes_onchain(monitor, labels):
    client = _install_chain(monitor, labels, [[]])
    monitor.run_once()
    assert client.calls and monitor.last_onchain_poll_at is not None


# -- formatting ----------------------------------------------------------------
def test_format_whale_alert_and_network_alert():
    transfer = Transfer(
        hash="abc", utime=time.time(), amount_ton=750_000, source=WALLET_A, destination=BINANCE,
        source_label=None, destination_label=Label("Binance", "CEX", "hot wallet"), kind="exchange_deposit",
        source_friendly="EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c", destination_friendly="EQBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
    )
    text = format_whale_alert(transfer, "negative", "medium", None)
    assert text.startswith("🐋 🔴")
    assert "750 тыс. TON" in text and "неизвестный кошелёк" in text and "Binance (hot wallet)" in text
    assert "EQAAAA…AM9c" in text and "Сила: средняя" in text
    assert "Информационный сигнал" in text

    stall = format_network_alert(600, 100)
    assert "Возможная остановка" in stall and "#100" in stall and "10 мин" in stall
    recovered = format_network_alert(600, 101, recovered=True)
    assert "снова" in recovered and "#101" in recovered
