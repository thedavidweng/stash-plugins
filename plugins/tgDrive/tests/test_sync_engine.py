import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger import Ledger
from sync_engine import SyncEngine


class TestSyncEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_ledger.sqlite")
        self.ledger = Ledger(self.db_path)

        self.mock_stash = MagicMock()
        self.mock_td = MagicMock()

    def tearDown(self):
        try:
            import shutil
            shutil.rmtree(self.temp_dir)
        except Exception:
            pass

    def test_sync_oversized_scene_skipped(self):
        def mock_find_scenes(page=1, per_page=40):
            if page == 1:
                return {
                    "count": 2,
                    "scenes": [
                        {
                            "id": "1",
                            "title": "Normal Scene",
                            "files": [{
                                "id": 101,
                                "path": "/data/normal.mp4",
                                "size": 1 * 1024 * 1024 * 1024,
                                "duration": 1800,
                                "width": 1920,
                                "height": 1080
                            }]
                        },
                        {
                            "id": "2",
                            "title": "Oversized Scene",
                            "files": [{
                                "id": 102,
                                "path": "/data/oversized.mp4",
                                "size": 5 * 1024 * 1024 * 1024,
                                "duration": 7200,
                                "width": 3840,
                                "height": 2160
                            }]
                        }
                    ]
                }
            return {"count": 2, "scenes": []}

        self.mock_stash.find_scenes.side_effect = mock_find_scenes
        self.mock_stash.download_image.return_value = False
        self.mock_td.upload_file.return_value = {
            "message_id": 1234,
            "hash": "blake3:abcd"
        }

        engine = SyncEngine(
            stash=self.mock_stash,
            td=self.mock_td,
            ledger=self.ledger,
            max_file_size_bytes=2 * 1024 * 1024 * 1024,
            dry_run=False,
        )

        report = engine.run_backup()

        self.assertEqual(report["scenes_total"], 2)
        self.assertEqual(report["scenes_published"], 1)
        self.assertEqual(report["scenes_skipped"], 1)
        self.assertEqual(len(report["skipped_details"]), 1)
        self.assertIn("Oversized", report["skipped_details"][0]["reason"])

        # Check ledger recorded the skip
        skipped_row = self.ledger.get("/data/oversized.mp4")
        self.assertIsNotNone(skipped_row)
        self.assertEqual(skipped_row["status"], "skipped")

        # Check normal scene was published
        pub_row = self.ledger.get("/data/normal.mp4")
        self.assertIsNotNone(pub_row)
        self.assertEqual(pub_row["status"], "published")
        self.assertEqual(pub_row["message_id"], 1234)

    def test_dry_run_mode(self):
        def mock_find_scenes(page=1, per_page=40):
            if page == 1:
                return {
                    "count": 1,
                    "scenes": [{
                        "id": "1",
                        "title": "Dry Scene",
                        "files": [{
                            "id": 101,
                            "path": "/data/dry.mp4",
                            "size": 500 * 1024 * 1024,
                            "duration": 600,
                        }]
                    }]
                }
            return {"count": 1, "scenes": []}

        self.mock_stash.find_scenes.side_effect = mock_find_scenes

        engine = SyncEngine(
            stash=self.mock_stash,
            td=self.mock_td,
            ledger=self.ledger,
            dry_run=True,
        )

        report = engine.run_backup()
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["scenes_published"], 1)
        # td.upload_file should NOT be called in dry run
        self.mock_td.upload_file.assert_not_called()
        # Ledger should not have recorded anything
        self.assertIsNone(self.ledger.get("/data/dry.mp4"))


if __name__ == "__main__":
    unittest.main()
