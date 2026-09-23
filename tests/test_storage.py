import os
import tempfile

from grambot.storage import Storage


def test_dedup_and_cluster_source_count():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        storage = Storage(db_path)
        try:
            assert not storage.has_seen("hash-1")
            storage.upsert_cluster("cluster-1", "Some title", 1000.0)
            storage.mark_seen("hash-1", "source-a", "Some title", None, 1000.0, "cluster-1")
            assert storage.has_seen("hash-1")

            storage.mark_seen("hash-2", "source-b", "Some title", None, 1005.0, "cluster-1")
            assert storage.cluster_source_count("cluster-1") == 2

            assert not storage.is_notified("cluster-1")
            storage.mark_notified("cluster-1")
            assert storage.is_notified("cluster-1")
        finally:
            storage.close()


def test_price_history_lookup():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        storage = Storage(db_path)
        try:
            storage.add_price_point(1.0, 100.0)
            latest = storage.latest_price()
            assert latest == (1.0, 100.0)
        finally:
            storage.close()
