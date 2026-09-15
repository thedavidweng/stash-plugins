import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger import Ledger
from settings import REMOTE_CONFIG_BACKUP, REMOTE_DATABASE_BACKUP, REMOTE_METADATA_EXPORT
from sync_engine import SyncEngine


def base_settings(**overrides):
    settings = {
        "backup_scenes": True,
        "backup_metadata": True,
        "backup_database": True,
        "backup_config": True,
        "max_file_size_gb": 2.0,
        "batch_size": 0,
    }
    settings.update(overrides)
    return settings


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

    # ------------------------------------------------------------- helpers

    def fake_download(self, url, dest, timeout=None):
        os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(b"artifact-bytes-0123456789")
        return dest

    # ------------------------------------------------------------- scenes

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
            settings=base_settings(),
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

    def test_dry_run_mode_touches_nothing(self):
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
            settings=base_settings(),
            dry_run=True,
        )

        report = engine.run_backup()
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["scenes_published"], 1)
        # No uploads, no GraphQL export/backup mutations, no ledger writes
        self.mock_td.upload_file.assert_not_called()
        self.mock_stash.export_objects.assert_not_called()
        self.mock_stash.backup_database.assert_not_called()
        self.mock_stash.get_config_file_path.assert_not_called()
        self.assertIsNone(self.ledger.get("/data/dry.mp4"))
        for slot in report["artifacts"].values():
            self.assertEqual(slot["status"], "skipped")

    def test_scene_backup_disabled(self):
        engine = SyncEngine(
            stash=self.mock_stash,
            td=self.mock_td,
            ledger=self.ledger,
            settings=base_settings(backup_scenes=False),
            dry_run=False,
        )
        report = engine.run_backup()
        self.mock_stash.find_scenes.assert_not_called()
        self.assertEqual(report["scenes_total"], 0)

    def test_batch_size_from_settings(self):
        def mock_find_scenes(page=1, per_page=40):
            if page == 1:
                return {
                    "count": 5,
                    "scenes": [
                        {"id": str(i), "title": f"Scene {i}",
                         "files": [{"id": 100 + i, "path": f"/data/s{i}.mp4", "size": 10}]}
                        for i in range(5)
                    ],
                }
            return {"count": 5, "scenes": []}

        self.mock_stash.find_scenes.side_effect = mock_find_scenes
        self.mock_td.upload_file.return_value = {"message_id": 1, "hash": "h"}

        engine = SyncEngine(
            stash=self.mock_stash,
            td=self.mock_td,
            ledger=self.ledger,
            settings=base_settings(batch_size=2),
            dry_run=False,
        )
        report = engine.run_backup()
        self.assertEqual(report["scenes_published"], 2)

    # ---------------------------------------------------------- artifacts

    def test_artifacts_backed_up_via_graphql(self):
        config_file = os.path.join(self.temp_dir, "config.yml")
        with open(config_file, "w") as f:
            f.write("stash:\n  test: true\n")

        self.mock_stash.export_objects.return_value = "http://stash:9999/downloads/a/export.zip"
        self.mock_stash.backup_database.return_value = "http://stash:9999/downloads/b/stash-backup.zip"
        self.mock_stash.supports_include_blobs.return_value = True
        self.mock_stash.get_config_file_path.return_value = config_file
        self.mock_stash.download_file.side_effect = self.fake_download
        self.mock_td.upload_file.return_value = {"message_id": 7, "hash": "blake3:x"}

        engine = SyncEngine(
            stash=self.mock_stash,
            td=self.mock_td,
            ledger=self.ledger,
            settings=base_settings(backup_scenes=False),
            dry_run=False,
        )
        report = engine.run_backup()

        # Metadata export via GraphQL
        self.mock_stash.export_objects.assert_called_once()
        # Database snapshot via GraphQL, blobs requested (server supports it)
        self.mock_stash.backup_database.assert_called_once_with(include_blobs=True)
        # Config located via GraphQL
        self.mock_stash.get_config_file_path.assert_called_once()

        upload_calls = self.mock_td.upload_file.call_args_list
        remote_paths = sorted(c.kwargs["remote_path"] for c in upload_calls)
        self.assertEqual(remote_paths, sorted([
            REMOTE_METADATA_EXPORT,
            REMOTE_DATABASE_BACKUP,
            REMOTE_CONFIG_BACKUP,
        ]))
        for call in upload_calls:
            self.assertTrue(call.kwargs.get("replace"))

        artifacts = report["artifacts"]
        self.assertEqual(artifacts["metadata_export"]["status"], "ok")
        self.assertEqual(artifacts["database_backup"]["status"], "ok")
        self.assertEqual(artifacts["config_backup"]["status"], "ok")

        # Artifacts recorded in the ledger for the Verify task
        for path in (REMOTE_METADATA_EXPORT, REMOTE_DATABASE_BACKUP, REMOTE_CONFIG_BACKUP):
            row = self.ledger.get(path)
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "published")

    def test_disabled_artifacts_not_attempted(self):
        self.mock_stash.supports_include_blobs.return_value = False
        engine = SyncEngine(
            stash=self.mock_stash,
            td=self.mock_td,
            ledger=self.ledger,
            settings=base_settings(
                backup_scenes=False, backup_metadata=False,
                backup_database=False, backup_config=False,
            ),
            dry_run=False,
        )
        report = engine.run_backup()
        self.mock_stash.export_objects.assert_not_called()
        self.mock_stash.backup_database.assert_not_called()
        self.mock_stash.get_config_file_path.assert_not_called()
        for slot in report["artifacts"].values():
            self.assertEqual(slot["status"], "disabled")

    def test_oversized_database_snapshot_guarded(self):
        config_file = os.path.join(self.temp_dir, "config.yml")
        with open(config_file, "w") as f:
            f.write("stash:\n  test: true\n")

        def big_download(url, dest, timeout=None):
            os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(b"x" * (4 * 1024 * 1024))  # 4 MiB snapshot
            return dest

        self.mock_stash.backup_database.return_value = "http://stash:9999/downloads/b/stash-backup.zip"
        self.mock_stash.supports_include_blobs.return_value = True
        self.mock_stash.get_config_file_path.return_value = config_file
        self.mock_stash.download_file.side_effect = big_download

        engine = SyncEngine(
            stash=self.mock_stash,
            td=self.mock_td,
            ledger=self.ledger,
            # 1 MiB ceiling: the 4 MiB snapshot is oversized
            settings=base_settings(
                backup_scenes=False, backup_metadata=False, backup_config=False,
                max_file_size_gb=0.0009765625,
            ),
            dry_run=False,
        )
        report = engine.run_backup()

        db_slot = report["artifacts"]["database_backup"]
        self.assertEqual(db_slot["status"], "skipped")
        self.assertIn("Oversized", db_slot["reason"])
        self.mock_td.upload_file.assert_not_called()
        row = self.ledger.get(REMOTE_DATABASE_BACKUP)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "skipped")

    def test_export_failure_reported_not_fatal(self):
        self.mock_stash.export_objects.side_effect = RuntimeError("Stash GraphQL HTTP 404")
        engine = SyncEngine(
            stash=self.mock_stash,
            td=self.mock_td,
            ledger=self.ledger,
            settings=base_settings(backup_scenes=False, backup_database=False, backup_config=False),
            dry_run=False,
        )
        report = engine.run_backup()
        self.assertEqual(report["artifacts"]["metadata_export"]["status"], "failed")
        self.assertIn("404", report["artifacts"]["metadata_export"]["error"])


if __name__ == "__main__":
    unittest.main()
