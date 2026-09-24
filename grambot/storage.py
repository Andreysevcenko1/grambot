"""SQLite-backed storage: dedup, clustering, price history, sent signals
(for backtesting), and small key/value state.

The connection is shared between the polling loop and the Telegram command
thread, so every access goes through a re-entrant lock.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_items (
    item_hash TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    published_at REAL NOT NULL,
    cluster_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_seen_items_cluster ON seen_items(cluster_id);
CREATE INDEX IF NOT EXISTS idx_seen_items_published ON seen_items(published_at);

CREATE TABLE IF NOT EXISTS clusters (
    cluster_id TEXT PRIMARY KEY,
    representative_title TEXT NOT NULL,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    notified INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS price_history (
    fetched_at REAL NOT NULL,
    price_usd REAL NOT NULL,
    volume_usd REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_history_fetched ON price_history(fetched_at);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    cluster_id TEXT,
    title TEXT NOT NULL,
    url TEXT,
    sentiment TEXT,
    strength TEXT,
    source_count INTEGER NOT NULL DEFAULT 0,
    sent_at REAL NOT NULL,
    price_at_send REAL,
    price_after_1h REAL,
    price_after_24h REAL
);
CREATE INDEX IF NOT EXISTS idx_signals_sent ON signals(sent_at);

CREATE TABLE IF NOT EXISTS onchain_transfers (
    hash TEXT PRIMARY KEY,
    utime REAL NOT NULL,
    source TEXT NOT NULL,
    destination TEXT NOT NULL,
    amount_ton REAL NOT NULL,
    source_label TEXT,
    destination_label TEXT,
    kind TEXT NOT NULL,
    notified INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_onchain_utime ON onchain_transfers(utime);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# (table, column, DDL) — applied when the column is missing so existing
# databases created by older versions keep working.
MIGRATIONS = [
    ("price_history", "change_24h_pct", "ALTER TABLE price_history ADD COLUMN change_24h_pct REAL"),
    ("price_history", "provider", "ALTER TABLE price_history ADD COLUMN provider TEXT"),
]


@dataclass
class ClusterState:
    cluster_id: str
    representative_title: str
    first_seen_at: float
    last_seen_at: float
    notified: bool
    source_count: int


@dataclass
class PricePoint:
    fetched_at: float
    price_usd: float
    volume_usd: float
    change_24h_pct: Optional[float] = None
    provider: Optional[str] = None


@dataclass
class SeenItem:
    source: str
    title: str
    url: Optional[str]
    published_at: float
    cluster_id: Optional[str]


@dataclass
class SignalRecord:
    id: int
    kind: str  # "news" | "price"
    cluster_id: Optional[str]
    title: str
    url: Optional[str]
    sentiment: Optional[str]
    strength: Optional[str]
    source_count: int
    sent_at: float
    price_at_send: Optional[float]
    price_after_1h: Optional[float]
    price_after_24h: Optional[float]


@dataclass
class SignalStats:
    total_news: int = 0
    total_price: int = 0
    total_onchain: int = 0
    evaluated_1h: int = 0
    hits_1h: int = 0
    avg_abs_move_1h: Optional[float] = None
    evaluated_24h: int = 0
    hits_24h: int = 0
    avg_abs_move_24h: Optional[float] = None


@dataclass
class TransferRecord:
    hash: str
    utime: float
    source: str
    destination: str
    amount_ton: float
    source_label: Optional[str]
    destination_label: Optional[str]
    kind: str
    notified: bool


@dataclass
class FlowStats:
    """Exchange flows over a window, in TON."""
    deposits_ton: float = 0.0
    withdrawals_ton: float = 0.0
    deposits_count: int = 0
    withdrawals_count: int = 0
    total_count: int = 0
    total_ton: float = 0.0

    @property
    def net_ton(self) -> float:
        """Positive = more left exchanges than arrived (accumulation)."""
        return self.withdrawals_ton - self.deposits_ton


def _row_to_signal(row: sqlite3.Row) -> SignalRecord:
    return SignalRecord(
        id=row["id"],
        kind=row["kind"],
        cluster_id=row["cluster_id"],
        title=row["title"],
        url=row["url"],
        sentiment=row["sentiment"],
        strength=row["strength"],
        source_count=row["source_count"],
        sent_at=row["sent_at"],
        price_at_send=row["price_at_send"],
        price_after_1h=row["price_after_1h"],
        price_after_24h=row["price_after_24h"],
    )


def _row_to_price(row: sqlite3.Row) -> PricePoint:
    return PricePoint(
        fetched_at=row["fetched_at"],
        price_usd=row["price_usd"],
        volume_usd=row["volume_usd"],
        change_24h_pct=row["change_24h_pct"],
        provider=row["provider"],
    )


class Storage:
    """Thin, thread-safe wrapper around a SQLite database file."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._configure_connection()
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)
            self._apply_migrations()

    def _configure_connection(self) -> None:
        # WAL survives crashes better and lets the command thread read while
        # the main loop writes; both pragmas are no-ops for ":memory:" DBs.
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
        except sqlite3.DatabaseError as exc:  # pragma: no cover - e.g. network filesystems
            logger.warning("Could not set SQLite pragmas: %s", exc)

    def _apply_migrations(self) -> None:
        for table, column, ddl in MIGRATIONS:
            columns = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                self._conn.execute(ddl)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _query(self, sql: str, params: Sequence = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _query_one(self, sql: str, params: Sequence = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _execute(self, sql: str, params: Sequence = ()) -> sqlite3.Cursor:
        with self._lock, self._conn:
            return self._conn.execute(sql, params)

    # -- dedup -------------------------------------------------------
    def has_seen(self, item_hash: str) -> bool:
        return self._query_one("SELECT 1 FROM seen_items WHERE item_hash = ?", (item_hash,)) is not None

    def mark_seen(
        self,
        item_hash: str,
        source: str,
        title: str,
        url: Optional[str],
        published_at: float,
        cluster_id: Optional[str],
    ) -> None:
        self._execute(
            """
            INSERT OR IGNORE INTO seen_items
                (item_hash, source, title, url, published_at, cluster_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (item_hash, source, title, url, published_at, cluster_id),
        )

    def recent_items(self, since_ts: float, limit: int = 10) -> List[SeenItem]:
        rows = self._query(
            """
            SELECT source, title, url, published_at, cluster_id FROM seen_items
            WHERE published_at >= ?
            ORDER BY published_at DESC LIMIT ?
            """,
            (since_ts, limit),
        )
        return [
            SeenItem(
                source=row["source"],
                title=row["title"],
                url=row["url"],
                published_at=row["published_at"],
                cluster_id=row["cluster_id"],
            )
            for row in rows
        ]

    def count_items_since(self, since_ts: float) -> int:
        row = self._query_one("SELECT COUNT(*) AS c FROM seen_items WHERE published_at >= ?", (since_ts,))
        return int(row["c"]) if row else 0

    # -- clustering ----------------------------------------------------
    def recent_clusters(self, since_ts: float) -> List[ClusterState]:
        rows = self._query(
            """
            SELECT c.cluster_id, c.representative_title, c.first_seen_at,
                   c.last_seen_at, c.notified, COUNT(DISTINCT s.source) AS source_count
            FROM clusters c
            LEFT JOIN seen_items s ON s.cluster_id = c.cluster_id
            WHERE c.last_seen_at >= ?
            GROUP BY c.cluster_id
            """,
            (since_ts,),
        )
        return [
            ClusterState(
                cluster_id=row["cluster_id"],
                representative_title=row["representative_title"],
                first_seen_at=row["first_seen_at"],
                last_seen_at=row["last_seen_at"],
                notified=bool(row["notified"]),
                source_count=row["source_count"],
            )
            for row in rows
        ]

    def upsert_cluster(self, cluster_id: str, title: str, ts: float) -> None:
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT cluster_id FROM clusters WHERE cluster_id = ?", (cluster_id,)
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    """
                    INSERT INTO clusters
                        (cluster_id, representative_title, first_seen_at, last_seen_at, notified)
                    VALUES (?, ?, ?, ?, 0)
                    """,
                    (cluster_id, title, ts, ts),
                )
            else:
                self._conn.execute(
                    "UPDATE clusters SET last_seen_at = MAX(last_seen_at, ?) WHERE cluster_id = ?",
                    (ts, cluster_id),
                )

    def cluster_sources(self, cluster_id: str) -> List[str]:
        rows = self._query(
            "SELECT DISTINCT source FROM seen_items WHERE cluster_id = ? ORDER BY published_at",
            (cluster_id,),
        )
        return [row["source"] for row in rows]

    def cluster_source_count(self, cluster_id: str) -> int:
        row = self._query_one(
            "SELECT COUNT(DISTINCT source) AS c FROM seen_items WHERE cluster_id = ?", (cluster_id,)
        )
        return int(row["c"]) if row else 0

    def mark_notified(self, cluster_id: str) -> None:
        self._execute("UPDATE clusters SET notified = 1 WHERE cluster_id = ?", (cluster_id,))

    def is_notified(self, cluster_id: str) -> bool:
        row = self._query_one("SELECT notified FROM clusters WHERE cluster_id = ?", (cluster_id,))
        return bool(row["notified"]) if row else False

    # -- price history ---------------------------------------------------
    def add_price_point(
        self,
        price_usd: float,
        volume_usd: float,
        change_24h_pct: Optional[float] = None,
        fetched_at: Optional[float] = None,
        provider: str = "coingecko",
    ) -> None:
        self._execute(
            """
            INSERT INTO price_history (fetched_at, price_usd, volume_usd, change_24h_pct, provider)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                fetched_at if fetched_at is not None else time.time(),
                price_usd,
                volume_usd,
                change_24h_pct,
                provider,
            ),
        )

    def price_at_or_before(self, ts: float) -> Optional[PricePoint]:
        row = self._query_one(
            """
            SELECT fetched_at, price_usd, volume_usd, change_24h_pct, provider FROM price_history
            WHERE fetched_at <= ?
            ORDER BY fetched_at DESC LIMIT 1
            """,
            (ts,),
        )
        return _row_to_price(row) if row else None

    def price_near(self, ts: float, tolerance_seconds: float) -> Optional[PricePoint]:
        """Closest stored point within ``tolerance_seconds`` of ``ts``, if any."""
        row = self._query_one(
            """
            SELECT fetched_at, price_usd, volume_usd, change_24h_pct, provider FROM price_history
            WHERE fetched_at BETWEEN ? AND ?
            ORDER BY ABS(fetched_at - ?) ASC LIMIT 1
            """,
            (ts - tolerance_seconds, ts + tolerance_seconds, ts),
        )
        return _row_to_price(row) if row else None

    def latest_price(self) -> Optional[PricePoint]:
        row = self._query_one(
            "SELECT fetched_at, price_usd, volume_usd, change_24h_pct, provider FROM price_history "
            "ORDER BY fetched_at DESC LIMIT 1"
        )
        return _row_to_price(row) if row else None

    # -- signals (sent notifications, used for backtesting) ------------
    def record_signal(
        self,
        kind: str,
        title: str,
        url: Optional[str],
        sentiment: Optional[str],
        strength: Optional[str],
        source_count: int,
        price_at_send: Optional[float],
        cluster_id: Optional[str] = None,
        sent_at: Optional[float] = None,
    ) -> int:
        cur = self._execute(
            """
            INSERT INTO signals
                (kind, cluster_id, title, url, sentiment, strength, source_count, sent_at, price_at_send)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                kind,
                cluster_id,
                title,
                url,
                sentiment,
                strength,
                source_count,
                sent_at if sent_at is not None else time.time(),
                price_at_send,
            ),
        )
        return int(cur.lastrowid)

    def count_signals_since(self, since_ts: float) -> int:
        row = self._query_one("SELECT COUNT(*) AS c FROM signals WHERE sent_at >= ?", (since_ts,))
        return int(row["c"]) if row else 0

    def recent_signals(self, limit: int = 5) -> List[SignalRecord]:
        rows = self._query("SELECT * FROM signals ORDER BY sent_at DESC LIMIT ?", (limit,))
        return [_row_to_signal(row) for row in rows]

    def signals_missing_followup(self, column: str, sent_before: float, sent_after: float) -> List[SignalRecord]:
        if column not in {"price_after_1h", "price_after_24h"}:
            raise ValueError(f"unsupported follow-up column: {column}")
        rows = self._query(
            f"""
            SELECT * FROM signals
            WHERE {column} IS NULL AND price_at_send IS NOT NULL
              AND sent_at <= ? AND sent_at >= ?
            ORDER BY sent_at
            """,
            (sent_before, sent_after),
        )
        return [_row_to_signal(row) for row in rows]

    def set_signal_followup(self, signal_id: int, column: str, price: float) -> None:
        if column not in {"price_after_1h", "price_after_24h"}:
            raise ValueError(f"unsupported follow-up column: {column}")
        self._execute(f"UPDATE signals SET {column} = ? WHERE id = ?", (price, signal_id))

    def signal_stats(self) -> SignalStats:
        stats = SignalStats()
        row = self._query_one("SELECT COUNT(*) AS c FROM signals WHERE kind = 'price'")
        stats.total_price = int(row["c"]) if row else 0
        row = self._query_one("SELECT COUNT(*) AS c FROM signals WHERE kind = 'onchain'")
        stats.total_onchain = int(row["c"]) if row else 0
        rows = self._query(
            "SELECT sentiment, price_at_send, price_after_1h, price_after_24h FROM signals WHERE kind = 'news'"
        )
        stats.total_news = len(rows)

        def evaluate(column: str):
            evaluated = hits = 0
            moves: List[float] = []
            for r in rows:
                if r["sentiment"] not in ("positive", "negative"):
                    continue
                base = r["price_at_send"]
                after = r[column]
                if not base or after is None:
                    continue
                change = (after - base) / base * 100.0
                expected = 1 if r["sentiment"] == "positive" else -1
                evaluated += 1
                if change * expected > 0:
                    hits += 1
                moves.append(abs(change))
            avg = sum(moves) / len(moves) if moves else None
            return evaluated, hits, avg

        stats.evaluated_1h, stats.hits_1h, stats.avg_abs_move_1h = evaluate("price_after_1h")
        stats.evaluated_24h, stats.hits_24h, stats.avg_abs_move_24h = evaluate("price_after_24h")
        return stats

    # -- on-chain transfers ------------------------------------------------
    def record_transfer(
        self,
        hash: str,
        utime: float,
        source: str,
        destination: str,
        amount_ton: float,
        source_label: Optional[str],
        destination_label: Optional[str],
        kind: str,
    ) -> bool:
        """Insert a transfer; returns False when the hash was already known."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO onchain_transfers
                    (hash, utime, source, destination, amount_ton, source_label, destination_label, kind)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (hash, utime, source, destination, amount_ton, source_label, destination_label, kind),
            )
            return cur.rowcount > 0

    def mark_transfer_notified(self, hash: str) -> None:
        self._execute("UPDATE onchain_transfers SET notified = 1 WHERE hash = ?", (hash,))

    def recent_transfers(self, since_ts: float, limit: int = 10, min_ton: float = 0.0) -> List[TransferRecord]:
        rows = self._query(
            """
            SELECT * FROM onchain_transfers
            WHERE utime >= ? AND amount_ton >= ?
            ORDER BY amount_ton DESC LIMIT ?
            """,
            (since_ts, min_ton, limit),
        )
        return [
            TransferRecord(
                hash=r["hash"],
                utime=r["utime"],
                source=r["source"],
                destination=r["destination"],
                amount_ton=r["amount_ton"],
                source_label=r["source_label"],
                destination_label=r["destination_label"],
                kind=r["kind"],
                notified=bool(r["notified"]),
            )
            for r in rows
        ]

    def flow_stats(self, since_ts: float) -> FlowStats:
        stats = FlowStats()
        rows = self._query(
            "SELECT kind, COUNT(*) AS c, COALESCE(SUM(amount_ton), 0) AS total "
            "FROM onchain_transfers WHERE utime >= ? GROUP BY kind",
            (since_ts,),
        )
        for r in rows:
            stats.total_count += int(r["c"])
            stats.total_ton += float(r["total"])
            if r["kind"] == "exchange_deposit":
                stats.deposits_count, stats.deposits_ton = int(r["c"]), float(r["total"])
            elif r["kind"] == "exchange_withdrawal":
                stats.withdrawals_count, stats.withdrawals_ton = int(r["c"]), float(r["total"])
        return stats

    # -- key/value state ---------------------------------------------------
    def get_value(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self._query_one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_value(self, key: str, value: Optional[str]) -> None:
        if value is None:
            self._execute("DELETE FROM kv WHERE key = ?", (key,))
        else:
            self._execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_float(self, key: str) -> Optional[float]:
        raw = self.get_value(key)
        if raw is None:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    # -- maintenance -------------------------------------------------------
    def prune(self, retention_days: int) -> int:
        """Delete old items, clusters and price points. Signals are kept."""
        cutoff = time.time() - retention_days * 86400
        with self._lock, self._conn:
            deleted = 0
            deleted += self._conn.execute("DELETE FROM seen_items WHERE published_at < ?", (cutoff,)).rowcount
            deleted += self._conn.execute("DELETE FROM clusters WHERE last_seen_at < ?", (cutoff,)).rowcount
            deleted += self._conn.execute("DELETE FROM price_history WHERE fetched_at < ?", (cutoff,)).rowcount
            deleted += self._conn.execute("DELETE FROM onchain_transfers WHERE utime < ?", (cutoff,)).rowcount
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.DatabaseError:  # pragma: no cover
                pass
        return deleted

    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += os.path.getsize(self.path + suffix)
            except OSError:
                pass
        return total
