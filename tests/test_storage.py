import os
import tempfile
import threading

import pytest

from grambot.storage import Storage


@pytest.fixture
def storage():
    with tempfile.TemporaryDirectory() as tmp:
        store = Storage(os.path.join(tmp, "test.db"))
        try:
            yield store
        finally:
            store.close()


def test_dedup_and_cluster_source_count(storage):
    assert not storage.has_seen("hash-1")
    storage.upsert_cluster("cluster-1", "Some title", 1000.0)
    storage.mark_seen("hash-1", "source-a", "Some title", None, 1000.0, "cluster-1")
    assert storage.has_seen("hash-1")

    storage.mark_seen("hash-2", "source-b", "Some title", None, 1005.0, "cluster-1")
    assert storage.cluster_source_count("cluster-1") == 2
    assert storage.cluster_sources("cluster-1") == ["source-a", "source-b"]

    assert not storage.is_notified("cluster-1")
    storage.mark_notified("cluster-1")
    assert storage.is_notified("cluster-1")

    clusters = storage.recent_clusters(0)
    assert len(clusters) == 1
    assert clusters[0].source_count == 2
    assert clusters[0].notified


def test_upsert_cluster_extends_last_seen(storage):
    storage.upsert_cluster("c", "t", 100.0)
    storage.upsert_cluster("c", "t", 200.0)
    storage.upsert_cluster("c", "t", 150.0)
    assert storage.recent_clusters(0)[0].last_seen_at == 200.0


def test_price_history_lookups(storage):
    storage.add_price_point(1.0, 100.0, change_24h_pct=2.5, fetched_at=1000.0)
    storage.add_price_point(1.1, 120.0, change_24h_pct=3.0, fetched_at=2000.0)
    latest = storage.latest_price()
    assert latest.price_usd == 1.1
    assert latest.change_24h_pct == 3.0
    assert storage.price_at_or_before(1500.0).price_usd == 1.0
    assert storage.price_at_or_before(500.0) is None
    assert storage.price_near(1900.0, tolerance_seconds=200).fetched_at == 2000.0
    assert storage.price_near(1500.0, tolerance_seconds=100) is None


def test_migration_adds_missing_column(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE price_history (fetched_at REAL NOT NULL, price_usd REAL NOT NULL, volume_usd REAL NOT NULL)")
    conn.execute("INSERT INTO price_history VALUES (1.0, 2.0, 3.0)")
    conn.commit()
    conn.close()

    store = Storage(str(path))
    try:
        latest = store.latest_price()
        assert latest.price_usd == 2.0
        assert latest.change_24h_pct is None
    finally:
        store.close()


def test_signals_followups_and_stats(storage):
    sid = storage.record_signal("news", "Bad news", None, "negative", "high", 2, price_at_send=10.0, sent_at=1000.0)
    storage.record_signal("news", "Good news", None, "positive", "medium", 2, price_at_send=10.0, sent_at=1100.0)
    storage.record_signal("price", "spike", None, "positive", "high", 0, price_at_send=10.0, sent_at=1200.0)

    assert storage.count_signals_since(1050.0) == 2
    pending = storage.signals_missing_followup("price_after_1h", sent_before=5000.0, sent_after=0.0)
    assert {s.id for s in pending} == {sid, sid + 1, sid + 2}

    storage.set_signal_followup(sid, "price_after_1h", 9.0)  # negative signal, price fell: hit
    storage.set_signal_followup(sid + 1, "price_after_1h", 9.5)  # positive signal, price fell: miss
    stats = storage.signal_stats()
    assert stats.total_news == 2
    assert stats.total_price == 1
    assert stats.evaluated_1h == 2
    assert stats.hits_1h == 1
    assert stats.avg_abs_move_1h == pytest.approx(7.5)
    assert stats.evaluated_24h == 0

    with pytest.raises(ValueError):
        storage.set_signal_followup(sid, "price_at_send", 1.0)


def test_kv_roundtrip(storage):
    assert storage.get_value("missing") is None
    assert storage.get_value("missing", "dflt") == "dflt"
    storage.set_value("k", "1.5")
    assert storage.get_float("k") == 1.5
    storage.set_value("k", "2")
    assert storage.get_value("k") == "2"
    storage.set_value("k", None)
    assert storage.get_value("k") is None


def test_prune_keeps_signals(storage):
    import time

    old = time.time() - 40 * 86400
    storage.upsert_cluster("old", "t", old)
    storage.mark_seen("h-old", "s", "t", None, old, "old")
    storage.add_price_point(1.0, 1.0, fetched_at=old)
    storage.record_signal("news", "t", None, "negative", "high", 2, price_at_send=1.0, sent_at=old)
    storage.mark_seen("h-new", "s", "t2", None, time.time(), None)

    deleted = storage.prune(retention_days=30)
    assert deleted == 3
    assert not storage.has_seen("h-old")
    assert storage.has_seen("h-new")
    assert storage.count_signals_since(0) == 1


def test_recent_items_and_counts(storage):
    storage.mark_seen("a", "src", "first", "http://a", 100.0, None)
    storage.mark_seen("b", "src", "second", None, 200.0, None)
    items = storage.recent_items(0, limit=1)
    assert [i.title for i in items] == ["second"]
    assert storage.count_items_since(150.0) == 1


def test_concurrent_access_is_safe(storage):
    errors = []

    def writer(prefix):
        try:
            for i in range(50):
                storage.mark_seen(f"{prefix}-{i}", "s", "t", None, float(i), None)
                storage.set_value("k", str(i))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(p,)) for p in ("a", "b", "c")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert storage.count_items_since(0) == 150
