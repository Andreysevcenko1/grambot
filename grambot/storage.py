"""SQLite-backed storage for dedup, price history and sent notifications."""
from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_items (
    item_hash TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    published_at REAL NOT NULL,
    cluster_id TEXT
);

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
"""


@dataclass
class ClusterState:
    cluster_id: str
    representative_title: str
    first_seen_at: float
    last_seen_at: float
    notified: bool
    source_count: int


class Storage:
    """Thin wrapper around a SQLite database file."""

    def __init__(self, path: str):
        self.path = path
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # -- dedup -------------------------------------------------------
    def has_seen(self, item_hash: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM seen_items WHERE item_hash = ?", (item_hash,)
        )
        return cur.fetchone() is not None

    def mark_seen(
        self,
        item_hash: str,
        source: str,
        title: str,
        url: Optional[str],
        published_at: float,
        cluster_id: Optional[str],
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO seen_items
                    (item_hash, source, title, url, published_at, cluster_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (item_hash, source, title, url, published_at, cluster_id),
            )

    # -- clustering ----------------------------------------------------
    def recent_clusters(self, since_ts: float) -> List[ClusterState]:
        cur = self._conn.execute(
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
            for row in cur.fetchall()
        ]

    def upsert_cluster(self, cluster_id: str, title: str, ts: float) -> None:
        with self._conn:
            existing = self._conn.execute(
                "SELECT cluster_id FROM clusters WHERE cluster_id = ?",
                (cluster_id,),
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
                    "UPDATE clusters SET last_seen_at = ? WHERE cluster_id = ?",
                    (ts, cluster_id),
                )

    def cluster_source_count(self, cluster_id: str) -> int:
        cur = self._conn.execute(
            "SELECT COUNT(DISTINCT source) AS c FROM seen_items WHERE cluster_id = ?",
            (cluster_id,),
        )
        row = cur.fetchone()
        return int(row["c"]) if row else 0

    def mark_notified(self, cluster_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE clusters SET notified = 1 WHERE cluster_id = ?", (cluster_id,)
            )

    def is_notified(self, cluster_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT notified FROM clusters WHERE cluster_id = ?", (cluster_id,)
        )
        row = cur.fetchone()
        return bool(row["notified"]) if row else False

    # -- price history ---------------------------------------------------
    def add_price_point(self, price_usd: float, volume_usd: float) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO price_history (fetched_at, price_usd, volume_usd) VALUES (?, ?, ?)",
                (time.time(), price_usd, volume_usd),
            )

    def price_at_or_before(self, ts: float) -> Optional[Tuple[float, float]]:
        cur = self._conn.execute(
            """
            SELECT price_usd, volume_usd FROM price_history
            WHERE fetched_at <= ?
            ORDER BY fetched_at DESC LIMIT 1
            """,
            (ts,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return row["price_usd"], row["volume_usd"]

    def latest_price(self) -> Optional[Tuple[float, float]]:
        cur = self._conn.execute(
            "SELECT price_usd, volume_usd FROM price_history ORDER BY fetched_at DESC LIMIT 1"
        )
        row = cur.fetchone()
        if row is None:
            return None
        return row["price_usd"], row["volume_usd"]
