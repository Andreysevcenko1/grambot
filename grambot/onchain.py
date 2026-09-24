"""On-chain signals for TON: large transfers and network health.

Data comes from the public TON Center v3 index (no key needed, ~1 request/s;
``TONCENTER_API_KEY`` lifts the limit). The message stream is scanned page by
page from the last seen timestamp and every internal message worth at least
``min_ton`` is turned into a :class:`Transfer`, with both sides labelled via the
ton-studio/ton-labels dataset (MIT) bundled in ``grambot/data/ton_labels.json``.

Network health is a plain check of the age of the latest masterchain block.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

USER_AGENT = "grambot/0.2 (+https://github.com/Andreysevcenko1/grambot)"
TONCENTER_BASE = "https://toncenter.com/api/v3"
DEFAULT_LABELS_URL = "https://raw.githubusercontent.com/shuva10v/ton-labels/build/assets.json"
BUNDLED_LABELS = Path(__file__).resolve().parent / "data" / "ton_labels.json"
LABELS_TTL_SECONDS = 24 * 3600
LABEL_CATEGORIES = {"CEX", "DEX", "bridge", "fund", "infrastructure", "lending", "liquid-staking", "validator"}

NANO = 1_000_000_000
PAGE_SIZE = 1000
DEFAULT_BACKOFF_SECONDS = 60.0
MAX_LAG_SECONDS = 30 * 60  # if the scan falls further behind, jump ahead and report a gap

# Elector, config, system/log and the minter: stake and fee flows, not transfers.
SYSTEM_ADDRESSES = {
    "-1:3333333333333333333333333333333333333333333333333333333333333333",
    "-1:5555555555555555555555555555555555555555555555555555555555555555",
    "-1:0000000000000000000000000000000000000000000000000000000000000000",
    "-1:FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF",
}
STAKING_CATEGORIES = {"validator", "liquid-staking"}

KIND_LABELS = {
    "exchange_deposit": "депозит на биржу — возможное давление продаж",
    "exchange_withdrawal": "вывод с биржи — часто холодное хранение или накопление",
    "exchange_to_exchange": "перевод между биржами",
    "exchange_internal": "внутренний перевод биржи",
    "dex": "крупная операция на DEX",
    "fund": "движение средств фонда / крупного холдера",
    "bridge": "перевод через мост",
    "staking": "стейкинг / валидаторы",
    "unknown": "перевод между неизвестными кошельками",
}
# Kinds worth an alert on their own; the rest are recorded for /whales only.
ALERT_KINDS = {"exchange_deposit", "exchange_withdrawal", "exchange_to_exchange", "dex", "fund", "unknown"}
KIND_SENTIMENT = {
    "exchange_deposit": "negative",
    "dex": "negative",
    "exchange_withdrawal": "positive",
}


class RateLimited(Exception):
    def __init__(self, retry_after: float):
        super().__init__(f"toncenter rate limited, retry after {retry_after:.0f}s")
        self.retry_after = retry_after


@dataclass
class Label:
    name: str
    category: str
    comment: str = ""

    @property
    def is_exchange(self) -> bool:
        return self.category == "CEX"

    def display(self) -> str:
        text = self.name
        if self.comment:
            text += f" ({self.comment})"
        return text


@dataclass
class Transfer:
    hash: str
    utime: float
    amount_ton: float
    source: str  # raw address "wc:HEX"
    destination: str
    source_label: Optional[Label]
    destination_label: Optional[Label]
    kind: str
    source_friendly: str = ""
    destination_friendly: str = ""

    @property
    def url(self) -> str:
        return f"https://tonviewer.com/transaction/{self.hash}"


@dataclass
class ScanResult:
    transfers: List[Transfer] = field(default_factory=list)
    last_utime: float = 0.0
    pages: int = 0
    messages: int = 0
    complete: bool = True
    gap_seconds: float = 0.0
    error: Optional[str] = None


@dataclass
class MasterchainState:
    seqno: int
    gen_utime: float
    fetched_at: float

    @property
    def age_seconds(self) -> float:
        return max(0.0, self.fetched_at - self.gen_utime)


# -- addresses ---------------------------------------------------------------
def _crc16(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


def raw_to_friendly(raw: str, bounceable: bool = True) -> str:
    """``0:HEX`` -> ``EQ...`` (url-safe base64 with CRC16, as explorers show it)."""
    workchain_str, hex_part = raw.split(":")
    workchain = int(workchain_str)
    tag = 0x11 if bounceable else 0x51
    payload = bytes([tag, workchain & 0xFF]) + bytes.fromhex(hex_part)
    payload += _crc16(payload).to_bytes(2, "big")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def normalize_raw(address: str) -> str:
    workchain, _, hex_part = address.partition(":")
    return f"{workchain}:{hex_part.upper()}"


# -- labels ------------------------------------------------------------------
class Labels:
    """Address -> :class:`Label` map with an optional daily refresh from GitHub."""

    def __init__(self, path: Optional[Path] = None, url: Optional[str] = DEFAULT_LABELS_URL, timeout: float = 30.0):
        self.path = path or BUNDLED_LABELS
        self.url = url
        self.timeout = timeout
        self._labels: Dict[str, Label] = {}
        self.loaded_at: float = 0.0
        self.refreshed_at: float = 0.0
        self.source = "none"
        self.load_bundled()

    def __len__(self) -> int:
        return len(self._labels)

    def get(self, raw_address: str) -> Optional[Label]:
        return self._labels.get(normalize_raw(raw_address))

    def load_bundled(self) -> int:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Could not load bundled labels from %s: %s", self.path, exc)
            return 0
        labels = {}
        for address, (name, category, comment) in data.get("addresses", {}).items():
            labels[normalize_raw(address)] = Label(name, category, comment)
        self._labels = labels
        self.loaded_at = time.time()
        self.source = "bundled"
        return len(labels)

    def refresh(self) -> int:
        """Download the latest dataset; keeps the current map on any failure."""
        if not self.url:
            return 0
        try:
            response = requests.get(self.url, headers={"User-Agent": USER_AGENT}, timeout=self.timeout)
            response.raise_for_status()
            labels = parse_labels_dataset(response.text)
        except (requests.RequestException, ValueError) as exc:
            logger.info("Labels refresh failed (%s); keeping %d %s labels", exc, len(self._labels), self.source)
            return 0
        if len(labels) < len(self._labels) // 2:
            logger.warning("Labels refresh returned only %d entries; ignoring", len(labels))
            return 0
        self._labels = labels
        self.refreshed_at = time.time()
        self.source = "remote"
        logger.info("Loaded %d TON address labels from %s", len(labels), self.url)
        return len(labels)

    def refresh_if_stale(self, ttl: float = LABELS_TTL_SECONDS) -> bool:
        if time.time() - self.refreshed_at < ttl:
            return False
        self.refresh()
        self.refreshed_at = self.refreshed_at or time.time()  # don't retry every poll on failure
        return True


def parse_labels_dataset(text: str) -> Dict[str, Label]:
    """Parse the ton-labels ``assets.json`` (JSON lines or a JSON array)."""
    rows: List[dict]
    stripped = text.strip()
    if stripped.startswith("["):
        rows = json.loads(stripped)
    else:
        rows = [json.loads(line) for line in stripped.splitlines() if line.strip()]
    labels: Dict[str, Label] = {}
    for row in rows:
        address = row.get("address")
        category = row.get("category") or ""
        if not address or category not in LABEL_CATEGORIES:
            continue
        labels[normalize_raw(address)] = Label(
            name=row.get("name") or row.get("label") or "",
            category=category,
            comment=(row.get("comment") or "")[:60],
        )
    return labels


# -- classification ----------------------------------------------------------
def classify_transfer(source: Optional[Label], destination: Optional[Label]) -> str:
    src_cat = source.category if source else None
    dst_cat = destination.category if destination else None
    if src_cat in STAKING_CATEGORIES or dst_cat in STAKING_CATEGORIES:
        return "staking"
    if src_cat == "CEX" and dst_cat == "CEX":
        assert source and destination
        return "exchange_internal" if source.name.lower() == destination.name.lower() else "exchange_to_exchange"
    if dst_cat == "CEX":
        return "exchange_deposit"
    if src_cat == "CEX":
        return "exchange_withdrawal"
    if "DEX" in (src_cat, dst_cat):
        return "dex"
    if "bridge" in (src_cat, dst_cat):
        return "bridge"
    if "fund" in (src_cat, dst_cat):
        return "fund"
    return "unknown"


def transfer_sentiment(transfer: Transfer, min_ton: float) -> Tuple[str, str]:
    sentiment = KIND_SENTIMENT.get(transfer.kind, "unknown")
    strength = "high" if transfer.amount_ton >= 4 * min_ton else ("medium" if transfer.amount_ton >= 2 * min_ton else "low")
    return sentiment, strength


# -- TON Center client -------------------------------------------------------
class TonCenterClient:
    def __init__(self, api_key: str = "", base_url: str = TONCENTER_BASE, timeout: float = 30.0, min_spacing: float = 1.1):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.min_spacing = min_spacing if not api_key else 0.15
        self._last_request_at = 0.0
        self._session = requests.Session()
        self.backoff_until = 0.0
        self.last_error: Optional[str] = None

    def _get(self, path: str, params: dict) -> dict:
        wait = self._last_request_at + self.min_spacing - time.time()
        if wait > 0:
            time.sleep(wait)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        self._last_request_at = time.time()
        response = self._session.get(f"{self.base_url}{path}", params=params, headers=headers, timeout=self.timeout)
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                seconds = float(retry_after) if retry_after else DEFAULT_BACKOFF_SECONDS
            except ValueError:
                seconds = DEFAULT_BACKOFF_SECONDS
            raise RateLimited(seconds)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("unexpected toncenter payload")
        return payload

    def masterchain_state(self) -> MasterchainState:
        payload = self._get("/masterchainInfo", {})
        last = payload["last"]
        return MasterchainState(seqno=int(last["seqno"]), gen_utime=float(last["gen_utime"]), fetched_at=time.time())

    def messages(self, start_utime: int, end_utime: Optional[int], offset: int, limit: int = PAGE_SIZE) -> Tuple[List[dict], Dict[str, dict]]:
        params = {
            "start_utime": start_utime,
            "exclude_externals": "true",
            "limit": limit,
            "offset": offset,
            "sort": "asc",
        }
        if end_utime is not None:
            params["end_utime"] = end_utime
        payload = self._get("/messages", params)
        return list(payload.get("messages") or []), dict(payload.get("address_book") or {})


# -- scanning ----------------------------------------------------------------
def message_to_transfer(message: dict, labels: Labels, address_book: Dict[str, dict], min_nano: int) -> Optional[Transfer]:
    if message.get("bounced"):
        return None
    source = message.get("source")
    destination = message.get("destination")
    if not source or not destination:
        return None
    try:
        value = int(message.get("value") or 0)
    except (TypeError, ValueError):
        return None
    if value < min_nano:
        return None
    source = normalize_raw(source)
    destination = normalize_raw(destination)
    if source in SYSTEM_ADDRESSES or destination in SYSTEM_ADDRESSES:
        return None
    src_label = labels.get(source)
    dst_label = labels.get(destination)

    def friendly(raw: str) -> str:
        entry = address_book.get(raw) or address_book.get(raw.lower()) or {}
        return entry.get("user_friendly") or raw_to_friendly(raw)

    return Transfer(
        hash=message.get("hash") or "",
        utime=float(message.get("created_at") or 0),
        amount_ton=value / NANO,
        source=source,
        destination=destination,
        source_label=src_label,
        destination_label=dst_label,
        kind=classify_transfer(src_label, dst_label),
        source_friendly=friendly(source),
        destination_friendly=friendly(destination),
    )


def scan_transfers(
    client: TonCenterClient,
    labels: Labels,
    since_utime: float,
    min_ton: float,
    max_pages: int = 6,
    now: Optional[float] = None,
) -> ScanResult:
    """Scan messages created after ``since_utime`` (exclusive, whole seconds).

    Pages are read in chronological order until fewer than a full page comes
    back or ``max_pages`` is hit; in the latter case ``complete`` is False and
    ``last_utime`` points at the last message seen so the next scan resumes.
    If the scan is more than ``MAX_LAG_SECONDS`` behind it jumps ahead and
    reports the skipped span as ``gap_seconds``.
    """
    now = now or time.time()
    result = ScanResult(last_utime=since_utime)
    if now - since_utime > MAX_LAG_SECONDS:
        result.gap_seconds = now - since_utime - MAX_LAG_SECONDS
        since_utime = now - MAX_LAG_SECONDS
        result.last_utime = since_utime
    start = int(since_utime) + 1
    end = int(now) - 5  # let the index settle
    if end < start:
        return result
    min_nano = int(min_ton * NANO)
    offset = 0
    seen_hashes = set()
    while result.pages < max_pages:
        try:
            rows, address_book = client.messages(start, end, offset)
        except RateLimited as exc:
            client.backoff_until = time.time() + min(exc.retry_after, 3600.0)
            client.last_error = str(exc)
            result.error = str(exc)
            result.complete = False
            logger.info("toncenter rate limited; backing off %.0fs", exc.retry_after)
            return result
        except (requests.RequestException, ValueError, KeyError) as exc:
            client.last_error = f"{exc}"
            result.error = str(exc)
            result.complete = False
            logger.warning("toncenter messages failed: %s", exc)
            return result
        result.pages += 1
        result.messages += len(rows)
        for message in rows:
            created = float(message.get("created_at") or 0)
            if created > result.last_utime:
                result.last_utime = created
            transfer = message_to_transfer(message, labels, address_book, min_nano)
            if transfer and transfer.hash not in seen_hashes:
                seen_hashes.add(transfer.hash)
                result.transfers.append(transfer)
        if len(rows) < PAGE_SIZE:
            result.last_utime = max(result.last_utime, float(end))
            return result
        offset += PAGE_SIZE
    # Page cap hit: resume from the last message seen (it may be mid-second,
    # so step back one second and rely on hash de-duplication downstream).
    result.complete = False
    result.last_utime = max(since_utime, result.last_utime - 1)
    return result
