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
        self.ledger.record_success("/data/v1.mp4", "scene", 1000, 1, "h1", 10)
        self.ledger.record_skip("/data/v2.mp4", "scene", 5000, "Oversized file", 2)
        self.ledger.record_failure("/data/v3.mp4", "scene", 2000, "Network error", 3)

        summary = self.ledger.get_summary()
        self.assertEqual(summary["total_count"], 3)
        self.assertEqual(summary["published_count"], 1)
        self.assertEqual(summary["published_bytes"], 1000)
        self.assertEqual(summary["skipped_count"], 1)
        self.assertEqual(summary["skipped_bytes"], 5000)
        self.assertEqual(summary["failed_count"], 1)
        self.assertEqual(summary["failed_bytes"], 2000)


if __name__ == "__main__":
    unittest.main()
