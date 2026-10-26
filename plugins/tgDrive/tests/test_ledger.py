import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger import Ledger


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_ledger.sqlite")
        self.ledger = Ledger(self.db_path)

    def tearDown(self):
        try:
            import shutil
            shutil.rmtree(self.temp_dir)
        except Exception:
            pass

    def test_record_and_get(self):
        path = "/data/videos/scene1.mp4"
        self.assertIsNone(self.ledger.get(path))

        self.ledger.record_success(
            canonical_path=path,
            kind="scene",
            size=1024000,
            file_id=1,
            blake3="blake3:abc123",
            message_id=42,
        )

        item = self.ledger.get(path)
        self.assertIsNotNone(item)
        self.assertEqual(item["canonical_path"], path)
        self.assertEqual(item["status"], "published")
        self.assertEqual(item["message_id"], 42)
        self.assertEqual(item["size"], 1024000)
        self.assertEqual(item["blake3"], "blake3:abc123")

    def test_record_skip_and_summary(self):
        self.ledger.record_success("/data/v1.mp4", "scene:archive", 1000, 1, "h1", 10)
        self.ledger.record_skip("/data/v2.mp4", "scene:archive", 5000, "Oversized file", 2)
        self.ledger.record_failure("/data/v3.mp4", "scene:archive", 2000, "Network error", 3)

        summary = self.ledger.get_summary()
        self.assertEqual(summary["total_count"], 3)
        self.assertEqual(summary["published_count"], 1)
        self.assertEqual(summary["published_bytes"], 1000)
        self.assertEqual(summary["skipped_count"], 1)
        self.assertEqual(summary["skipped_bytes"], 5000)
        self.assertEqual(summary["failed_count"], 1)
        self.assertEqual(summary["failed_bytes"], 2000)
        self.assertEqual(
            summary["published_by_kind"],
            {"scene:archive": {"count": 1, "bytes": 1000}},
        )

    def test_remote_path_defaults_to_canonical_and_overrides(self):
        # archive rows mirror their remote path exactly
        self.ledger.record_success("/data/v1.mp4", "scene:archive", 1000, blake3="h1", message_id=10)
        # browse rows store a remote path that differs from the ledger key
        self.ledger.record_success(
            "browse:/data/v1.mp4", "scene:browse", 1000,
            blake3="h2", message_id=11, remote_path="/stash-browse/9/v1.mp4",
        )
        self.assertEqual(self.ledger.get("/data/v1.mp4")["remote_path"], "/data/v1.mp4")
        browse = self.ledger.get("browse:/data/v1.mp4")
        self.assertEqual(browse["remote_path"], "/stash-browse/9/v1.mp4")
        # re-recording updates the remote path
        self.ledger.record_success(
            "browse:/data/v1.mp4", "scene:browse", 1000,
            remote_path="/stash-browse/9/new-name.mp4",
        )
        self.assertEqual(
            self.ledger.get("browse:/data/v1.mp4")["remote_path"],
            "/stash-browse/9/new-name.mp4",
        )
    def test_fingerprint_stored_and_upserted(self):
        self.ledger.record_success(
            "/data/v1.mp4", "scene:archive", 1000,
            blake3="h1", message_id=10, fingerprint="md5=old",
        )
        row = self.ledger.get("/data/v1.mp4")
        self.assertEqual(row["fingerprint"], "md5=old")
        # re-publishing changed content updates the fingerprint
        self.ledger.record_success(
            "/data/v1.mp4", "scene:archive", 1200,
            blake3="h2", message_id=11, fingerprint="md5=new",
        )
        row = self.ledger.get("/data/v1.mp4")
        self.assertEqual(row["fingerprint"], "md5=new")
        self.assertEqual(row["size"], 1200)
    def test_legacy_ledger_gains_fingerprint_column(self):
        import sqlite3
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DROP TABLE ledger_items")
            conn.execute("""
                CREATE TABLE ledger_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id INTEGER,
                    canonical_path TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    blake3 TEXT,
                    message_id INTEGER,
                    status TEXT NOT NULL,
                    reason TEXT,
                    updated_at TEXT NOT NULL,
                    remote_path TEXT
                )
            """)
            conn.execute(
                "INSERT INTO ledger_items (canonical_path, kind, size, status, updated_at) "
                "VALUES ('/data/old.mp4', 'scene:archive', 5, 'published', '2026-01-01')"
            )
        migrated = Ledger(self.db_path)
        # a row recorded before fingerprint existed reads back with NULL
        row = migrated.get("/data/old.mp4")
        self.assertIsNone(row["fingerprint"])
        # and the migrated column accepts a fingerprint on re-record
        migrated.record_success(
            "/data/old.mp4", "scene:archive", 5,
            blake3="h1", message_id=10, fingerprint="md5=x",
        )
        self.assertEqual(migrated.get("/data/old.mp4")["fingerprint"], "md5=x")

    def test_legacy_ledger_gains_remote_path_column(self):
        # a ledger created before remote_path existed must migrate in place
        import sqlite3
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DROP TABLE ledger_items")
            conn.execute("""
                CREATE TABLE ledger_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id INTEGER,
                    canonical_path TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    blake3 TEXT,
                    message_id INTEGER,
                    status TEXT NOT NULL,
                    reason TEXT,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute(
                "INSERT INTO ledger_items (canonical_path, kind, size, status, updated_at) "
                "VALUES ('/data/old.mp4', 'scene:archive', 5, 'published', '2026-01-01')"
            )
        migrated = Ledger(self.db_path)
        row = migrated.get("/data/old.mp4")
        self.assertIsNone(row["remote_path"])
        # new writes backfill the column on the migrated table
        migrated.record_success("/data/new.mp4", "scene:archive", 7)
        self.assertEqual(migrated.get("/data/new.mp4")["remote_path"], "/data/new.mp4")


if __name__ == "__main__":
    unittest.main()
