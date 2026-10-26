import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger import Ledger
from settings import REMOTE_CONFIG_BACKUP, REMOTE_DATABASE_BACKUP, REMOTE_METADATA_EXPORT
from sync_engine import (
    SyncEngine,
    build_browse_remote_path,
    build_gallery_browse_display_name,
    resolve_local_path,
    scene_presentation,
)
from td_client import TDRateLimitedError, TDError


def base_settings(**overrides):
    settings = {
        "backup_scenes": True,
        "backup_images": True,
        "backup_galleries": True,
        "backup_metadata": True,
        "backup_database": True,
        "backup_config": True,
        "media_upload_mode": "archive",
        "scene_selection": "all",
        "max_file_size_gb": 2.0,
        "batch_size": 0,
        "export_timeout_seconds": 1800,
    }
    settings.update(overrides)
    return settings


class TestResolveLocalPath(unittest.TestCase):
    def test_exact_path_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "exact.mp4")
            with open(target, "wb") as f:
                f.write(b"x")
            self.assertEqual(resolve_local_path(target), target)

    def test_relative_path_resolves_to_root_prefix(self):
        """Stash paths like 'data/Porn/...' map to '/data/Porn/...' when the
        relative form is missing but the root-prefixed form exists."""
        real_exists = os.path.exists

        def fake_exists(path):
            if path == "/data/Porn/tmp/x.mp4":
                return True
            return real_exists(path)

        with tempfile.TemporaryDirectory() as tmp:
            rel = "data/Porn/tmp/x.mp4"
            with patch("os.path.exists", side_effect=fake_exists):
                self.assertEqual(resolve_local_path(rel), f"/{rel}")

    def test_missing_path_passes_through(self):
        self.assertEqual(resolve_local_path("no/such/file.mp4"), "no/such/file.mp4")
        self.assertEqual(resolve_local_path(""), "")


class TestSyncEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_ledger.sqlite")
        self.ledger = Ledger(self.db_path)

        self.mock_stash = MagicMock()
        self.mock_td = MagicMock()
        # Empty image/gallery library unless a test provides one, so the
        # legacy scene tests keep exercising the scene path only.
        self.mock_stash.find_images.return_value = {"count": 0, "images": []}
        self.mock_stash.find_galleries.return_value = {"count": 0, "galleries": []}

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
        self.assertEqual(self.mock_td.upload_file.call_args.kwargs["as_kind"], "document")

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

    def test_browse_path_contains_scene_metadata_and_hashtags(self):
        path = build_browse_remote_path(
            {
                "id": "42",
                "title": "Example / Scene",
                "code": "ABC-123",
                "date": "2025-02-03",
                "studio": {"name": "Example Studio"},
                "performers": [{"name": "Jane Doe"}],
                "tags": [{"name": "Tag One"}, {"name": "中文 标签"}],
            },
            "/data/example.mp4",
            duration=3723,
            width=1920,
            height=1080,
        )
        self.assertTrue(path.startswith("/stash-browse/42/"))
        self.assertIn("Example ∕ Scene", path)
        self.assertIn("ABC-123 · 2025-02-03 · 1:02:03 · 1920x1080", path)
        self.assertIn("#Example_Studio", path)
        self.assertIn("#Jane_Doe", path)
        self.assertIn("#中文_标签", path)
        self.assertTrue(path.endswith(".mp4"))

    def test_browse_mode_uploads_streamable_video_with_caption_path(self):
        self.mock_stash.find_scenes.return_value = {
            "count": 1,
            "scenes": [{
                "id": "9",
                "title": "Browse Me",
                "studio": {"name": "Studio"},
                "performers": [{"name": "Performer"}],
                "tags": [{"name": "Tag"}],
                "files": [{
                    "id": 91, "path": "/data/browse.mp4", "size": 1024,
                    "duration": 90, "width": 1280, "height": 720,
                }],
            }],
        }
        self.mock_td.upload_file.return_value = {"message_id": 9, "hash": "h"}
        engine = SyncEngine(
            stash=self.mock_stash,
            td=self.mock_td,
            ledger=self.ledger,
            settings=base_settings(
                backup_images=False, backup_galleries=False,
                backup_metadata=False, backup_database=False, backup_config=False,
                media_upload_mode="browse",
            ),
        )

        report = engine.run_backup()

        self.assertEqual(report["scenes_published"], 1)
        call = self.mock_td.upload_file.call_args.kwargs
        self.assertEqual(call["as_kind"], "video")
        self.assertTrue(call["streaming"])
        self.assertTrue(call["remote_path"].startswith("/stash-browse/9/"))
        self.assertIn("#Performer", call["remote_path"])
        self.assertEqual(self.ledger.get("browse:/data/browse.mp4")["kind"], "scene:browse")

    def test_browse_mode_uploads_images_as_photos(self):
        self.assertEqual(scene_presentation("/data/cover.JPG", "browse"), "photo")
        self.assertEqual(scene_presentation("/data/clip.mp4", "browse"), "video")
        self.assertEqual(scene_presentation("/data/clip.mp4", "archive"), "document")

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

    def test_fileless_scene_reported_not_silently_dropped(self):
        def mock_find_scenes(page=1, per_page=40):
            if page == 1:
                return {
                    "count": 2,
                    "scenes": [
                        {
                            "id": "1",
                            "title": "Real Scene",
                            "files": [{"id": 101, "path": "/data/real.mp4", "size": 10}],
                        },
                        {"id": "2", "title": "Ghost Scene", "files": []},
                    ],
                }
            return {"count": 2, "scenes": []}

        self.mock_stash.find_scenes.side_effect = mock_find_scenes
        self.mock_td.upload_file.return_value = {"message_id": 1, "hash": "h"}

        engine = SyncEngine(
            stash=self.mock_stash,
            td=self.mock_td,
            ledger=self.ledger,
            settings=base_settings(),
            dry_run=False,
        )
        report = engine.run_backup()

        # the fileless scene lands in a bucket so coverage math stays honest
        self.assertEqual(report["scenes_published"], 1)
        self.assertEqual(report["scenes_skipped"], 1)
        self.assertEqual(len(report["skipped_details"]), 1)
        self.assertEqual(report["skipped_details"][0]["scene_id"], "2")
        self.assertIn("no files", report["skipped_details"][0]["reason"])
        self.assertEqual(report["coverage_objects_percent"], 50.0)

    @patch("time.sleep")
    def test_scene_upload_retries_on_rate_limit(self, mock_sleep):
        """A per-send flood wait on a fresh session must not fail the scene."""
        def mock_find_scenes(page=1, per_page=40):
            if page == 1:
                return {
                    "count": 1,
                    "scenes": [{
                        "id": "7",
                        "title": "Flooded",
                        "files": [{"id": 71, "path": "/data/flooded.mp4", "size": 1024}],
                    }],
                }
            return {"count": 1, "scenes": []}

        self.mock_stash.find_scenes.side_effect = mock_find_scenes
        self.mock_stash.download_image.return_value = False
        self.mock_td.upload_file.side_effect = [
            TDRateLimitedError("telegram rate limited this account: retry after 2s", 2),
            {"message_id": 4242, "hash": "blake3:s"},
        ]

        engine = SyncEngine(
            stash=self.mock_stash,
            td=self.mock_td,
            ledger=self.ledger,
            settings=base_settings(backup_metadata=False, backup_database=False, backup_config=False),
            dry_run=False,
        )
        report = engine.run_backup()

        self.assertEqual(report["scenes_published"], 1)
        self.assertEqual(report["scenes_failed"], 0)
        self.assertEqual(self.mock_td.upload_file.call_count, 2)
        self.assertEqual(mock_sleep.call_count, 1)
        row = self.ledger.get("/data/flooded.mp4")
        self.assertEqual(row["status"], "published")
        self.assertEqual(row["message_id"], 4242)

    @patch("time.sleep")
    def test_scene_upload_fails_after_persistent_rate_limit(self, mock_sleep):
        def mock_find_scenes(page=1, per_page=40):
            if page == 1:
                return {
                    "count": 1,
                    "scenes": [{
                        "id": "8",
                        "title": "Doomed",
                        "files": [{"id": 81, "path": "/data/doomed.mp4", "size": 512}],
                    }],
                }
            return {"count": 1, "scenes": []}

        self.mock_stash.find_scenes.side_effect = mock_find_scenes
        self.mock_stash.download_image.return_value = False
        self.mock_td.upload_file.side_effect = TDRateLimitedError(
            "telegram rate limited this account: retry after 2s", 2
        )

        engine = SyncEngine(
            stash=self.mock_stash,
            td=self.mock_td,
            ledger=self.ledger,
            settings=base_settings(backup_metadata=False, backup_database=False, backup_config=False),
            dry_run=False,
        )
        report = engine.run_backup()

        self.assertEqual(report["scenes_published"], 0)
        self.assertEqual(report["scenes_failed"], 1)
        self.assertEqual(self.mock_td.upload_file.call_count, 3)
        row = self.ledger.get("/data/doomed.mp4")
        self.assertEqual(row["status"], "failed")
        self.assertIn("rate limited", row["reason"])

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

    @patch("time.sleep")
    def test_artifact_upload_retries_on_rate_limit(self, mock_sleep):
        """A short Telegram flood wait must not lose the artifact for the run."""
        self.mock_stash.export_objects.return_value = "http://stash:9999/downloads/a/export.zip"
        self.mock_stash.download_file.side_effect = self.fake_download
        self.mock_td.upload_file.side_effect = [
            TDRateLimitedError("telegram rate limited this account: retry after 2s", 2),
            TDRateLimitedError("telegram rate limited this account: retry after 2s", 2),
            {"message_id": 55, "hash": "blake3:ok"},
        ]

        engine = SyncEngine(
            stash=self.mock_stash,
            td=self.mock_td,
            ledger=self.ledger,
            settings=base_settings(backup_scenes=False, backup_database=False, backup_config=False),
            dry_run=False,
        )
        report = engine.run_backup()

        self.assertEqual(report["artifacts"]["metadata_export"]["status"], "ok")
        self.assertEqual(self.mock_td.upload_file.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)
        mock_sleep.assert_called_with(2)
        row = self.ledger.get(REMOTE_METADATA_EXPORT)
        self.assertEqual(row["status"], "published")

    @patch("time.sleep")
    def test_artifact_upload_fails_after_persistent_rate_limit(self, mock_sleep):
        self.mock_stash.export_objects.return_value = "http://stash:9999/downloads/a/export.zip"
        self.mock_stash.download_file.side_effect = self.fake_download
        self.mock_td.upload_file.side_effect = TDRateLimitedError(
            "telegram rate limited this account: retry after 2s", 2
        )

        engine = SyncEngine(
            stash=self.mock_stash,
            td=self.mock_td,
            ledger=self.ledger,
            settings=base_settings(backup_scenes=False, backup_database=False, backup_config=False),
            dry_run=False,
        )
        report = engine.run_backup()

        slot = report["artifacts"]["metadata_export"]
        self.assertEqual(slot["status"], "failed")
        self.assertIn("rate limited", slot["error"])
        self.assertEqual(self.mock_td.upload_file.call_count, 3)
        row = self.ledger.get(REMOTE_METADATA_EXPORT)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "failed")


IMAGE_BYTES = b"jpeg-bytes-0123456789"


def make_image(image_id, path, size=100, galleries=None, **extra):
    image = {
        "id": str(image_id),
        "title": extra.pop("title", f"Image {image_id}"),
        "code": extra.pop("code", None),
        "date": extra.pop("date", None),
        "studio": extra.pop("studio", None),
        "performers": extra.pop("performers", []),
        "tags": extra.pop("tags", []),
        "visual_files": [{
            "id": 900 + int(image_id), "path": path, "size": size,
            "width": 800, "height": 600,
        }],
        "paths": {"image": f"http://stash:9999/image/{image_id}/image"},
        "galleries": [{"id": str(g)} for g in (galleries or [])],
    }
    image.update(extra)
    return image


def write_file(path, content=IMAGE_BYTES):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content)
    return path


class TestGalleryBackup(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_ledger.sqlite")
        self.ledger = Ledger(self.db_path)

        self.mock_stash = MagicMock()
        self.mock_td = MagicMock()
        self.mock_stash.find_images.return_value = {"count": 0, "images": []}
        self.mock_stash.find_galleries.return_value = {"count": 0, "galleries": []}
        # Scenes stay out of the way; artifact flags off.
        self.settings = base_settings(
            backup_scenes=False, backup_images=False,
            backup_metadata=False, backup_database=False, backup_config=False,
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _engine(self, **setting_overrides):
        settings = dict(self.settings)
        settings.update(setting_overrides)
        return SyncEngine(
            stash=self.mock_stash, td=self.mock_td,
            ledger=self.ledger, settings=settings, dry_run=False,
        )

    def _album_result(self, names, prefix, start=11):
        return {
            "uploaded": len(names), "skipped": 0, "errors": [],
            "albums": [{
                "grouped_id": 7001, "reply_message_id": 99,
                "message_ids": [start + i for i in range(len(names))],
                "paths": [f"{prefix}{name}" for name in names],
            }],
            "channel_id": "-100123",
        }

    def _ls_result(self, prefix, names, hash_suffix="abc"):
        return [
            {"type": "file", "name": name, "path": f"{prefix}{name}",
             "size": 100, "hash": f"blake3:{hash_suffix}{i}"}
            for i, name in enumerate(names)
        ]

    # ------------------------------------------------------------- archive

    def test_folder_gallery_archive_publishes_document_album(self):
        base = os.path.join(self.temp_dir, "gals", "trip")
        names = ["a.jpg", "b.jpg", "c.jpg"]
        for name in names:
            write_file(os.path.join(base, name))
        remote_dir = base + "/"
        members = [
            make_image(i + 1, os.path.join(base, name), galleries=[1])
            for i, name in enumerate(names)
        ]
        self.mock_stash.find_images.return_value = {"count": 3, "images": members}
        self.mock_stash.find_galleries.return_value = {
            "count": 1,
            "galleries": [{
                "id": "1", "title": "Trip 2024", "image_count": 3,
                "studio": None, "performers": [], "tags": [],
                "folder": {"path": base}, "files": [],
            }],
        }
        self.mock_td.upload_album.return_value = self._album_result(names, remote_dir)
        self.mock_td.list_dir.return_value = self._ls_result(remote_dir, names)

        report = self._engine().run_backup()

        self.assertEqual(report["galleries_total"], 1)
        self.assertEqual(report["galleries_published"], 1)
        self.assertEqual(report["galleries_skipped"], 0)
        # one album invocation, members keep their library basenames so the
        # archive mirror stays intact
        args, kwargs = self.mock_td.upload_album.call_args
        self.assertEqual([os.path.basename(p) for p in args[0]], names)
        self.assertEqual(args[1], remote_dir)
        self.assertIsNone(kwargs["as_kind"])
        # per-member ledger rows with remote hashes read back via ls
        for name, i in zip(names, range(3)):
            row = self.ledger.get(os.path.join(base, name))
            self.assertEqual(row["status"], "published")
            self.assertEqual(row["kind"], "image:archive")
            self.assertEqual(row["remote_path"], f"{remote_dir}{name}")
            self.assertEqual(row["blake3"], f"blake3:abc{i}")
            self.assertEqual(row["message_id"], 11 + i)
        # coverage stays honest
        self.assertEqual(report["coverage_objects_percent"], 100.0)

    def test_folder_gallery_archive_lone_member_uploads_single_document(self):
        base = os.path.join(self.temp_dir, "gals", "solo")
        path = write_file(os.path.join(base, "only.png"))
        members = [make_image(5, path, galleries=[2])]
        self.mock_stash.find_images.return_value = {"count": 1, "images": members}
        self.mock_stash.find_galleries.return_value = {
            "count": 1,
            "galleries": [{
                "id": "2", "title": "Solo", "image_count": 1,
                "folder": {"path": base}, "files": [],
            }],
        }
        self.mock_td.upload_file.return_value = {"message_id": 3, "hash": "blake3:x"}

        report = self._engine().run_backup()

        self.mock_td.upload_album.assert_not_called()
        kwargs = self.mock_td.upload_file.call_args.kwargs
        self.assertEqual(kwargs["remote_path"], path)
        self.assertEqual(kwargs["as_kind"], "document")
        self.assertEqual(report["galleries_published"], 1)
        row = self.ledger.get(path)
        self.assertEqual(row["kind"], "image:archive")

    def test_zip_gallery_archive_publishes_zip_document_only(self):
        zip_path = write_file(
            os.path.join(self.temp_dir, "gals", "packed.zip"), b"zip-bytes"
        )
        # members of a zip gallery only exist inside the zip; their reported
        # paths are virtual and must not be uploaded individually
        members = [
            make_image(7, os.path.join(self.temp_dir, "gals", "packed.zip", "01.jpg"), galleries=[3]),
            make_image(8, os.path.join(self.temp_dir, "gals", "packed.zip", "02.jpg"), galleries=[3]),
        ]
        self.mock_stash.find_images.return_value = {"count": 2, "images": members}
        self.mock_stash.find_galleries.return_value = {
            "count": 1,
            "galleries": [{
                "id": "3", "title": "Packed", "image_count": 2,
                "files": [{"id": 55, "path": zip_path, "size": 9}],
            }],
        }
        self.mock_td.upload_file.return_value = {"message_id": 4, "hash": "blake3:z"}

        report = self._engine().run_backup()

        # the zip is the media unit: one document, members untouched
        self.assertEqual(self.mock_td.upload_file.call_count, 1)
        kwargs = self.mock_td.upload_file.call_args.kwargs
        self.assertEqual(kwargs["local_path"], zip_path)
        self.assertEqual(kwargs["remote_path"], zip_path)
        self.assertEqual(kwargs["as_kind"], "document")
        row = self.ledger.get(zip_path)
        self.assertEqual(row["kind"], "gallery:archive")
        self.assertEqual(row["status"], "published")
        self.assertEqual(report["galleries_published"], 1)
        self.assertEqual(report["bytes_published"], 9)

    def test_album_failure_records_every_member_failed(self):
        base = os.path.join(self.temp_dir, "gals", "trip")
        names = ["a.jpg", "b.jpg"]
        for name in names:
            write_file(os.path.join(base, name))
        members = [make_image(i + 1, os.path.join(base, name), galleries=[1]) for i, name in enumerate(names)]
        self.mock_stash.find_images.return_value = {"count": 2, "images": members}
        self.mock_stash.find_galleries.return_value = {
            "count": 1,
            "galleries": [{
                "id": "1", "title": "Trip", "image_count": 2,
                "folder": {"path": base}, "files": [],
            }],
        }
        self.mock_td.upload_album.side_effect = TDError("remote upload failed", code="ERR_TELEGRAM_RPC")

        report = self._engine().run_backup()

        self.assertEqual(report["galleries_failed"], 1)
        for name in names:
            row = self.ledger.get(os.path.join(base, name))
            self.assertEqual(row["status"], "failed")
            self.assertIn("remote upload failed", row["reason"])

    @patch("time.sleep")
    def test_album_retries_then_succeeds_after_rate_limit(self, mock_sleep):
        base = os.path.join(self.temp_dir, "gals", "trip")
        names = ["a.jpg", "b.jpg"]
        for name in names:
            write_file(os.path.join(base, name))
        members = [make_image(i + 1, os.path.join(base, name), galleries=[1]) for i, name in enumerate(names)]
        self.mock_stash.find_images.return_value = {"count": 2, "images": members}
        self.mock_stash.find_galleries.return_value = {
            "count": 1,
            "galleries": [{"id": "1", "title": "Trip", "image_count": 2,
                            "folder": {"path": base}, "files": []}],
        }
        self.mock_td.upload_album.side_effect = [
            TDRateLimitedError("telegram rate limited this account: retry after 2s", 2),
            self._album_result(names, base + "/"),
        ]
        self.mock_td.list_dir.return_value = self._ls_result(base + "/", names)

        report = self._engine().run_backup()

        self.assertEqual(report["galleries_published"], 1)
        self.assertEqual(self.mock_td.upload_album.call_count, 2)
        mock_sleep.assert_called_once_with(2)

    def test_oversized_gallery_member_dropped_not_album_fatal(self):
        base = os.path.join(self.temp_dir, "gals", "trip")
        small = write_file(os.path.join(base, "small.jpg"))
        big = write_file(os.path.join(base, "big.jpg"))
        members = [
            make_image(1, small, size=100, galleries=[1]),
            make_image(2, big, size=5 * 1024 ** 3, galleries=[1]),
        ]
        self.mock_stash.find_images.return_value = {"count": 2, "images": members}
        self.mock_stash.find_galleries.return_value = {
            "count": 1,
            "galleries": [{"id": "1", "title": "Trip", "image_count": 2,
                            "folder": {"path": base}, "files": []}],
        }
        self.mock_td.upload_file.return_value = {"message_id": 1, "hash": "blake3:s"}

        report = self._engine().run_backup()

        # the oversized member is skipped and the survivor still publishes
        self.assertEqual(report["galleries_skipped"], 1)
        self.assertEqual(self.mock_td.upload_album.call_count, 0)
        self.assertEqual(self.mock_td.upload_file.call_count, 1)
        self.assertEqual(self.ledger.get(big)["status"], "skipped")
        self.assertIn("Oversized", report["skipped_details"][0]["reason"])

    # -------------------------------------------------------------- browse

    def test_browse_gallery_publishes_photo_album_with_caption_member(self):
        base = os.path.join(self.temp_dir, "gals", "trip")
        names = ["a.jpg", "b.jpg", "c.jpg"]
        for name in names:
            write_file(os.path.join(base, name))
        members = [make_image(i + 1, os.path.join(base, name), galleries=[1]) for i, name in enumerate(names)]
        self.mock_stash.find_images.return_value = {"count": 3, "images": members}
        gallery = {
            "id": "1", "title": "Trip / 2024", "date": "2024-05-01",
            "image_count": 3, "code": None, "studio": {"name": "Studio"},
            "performers": [{"name": "Performer"}], "tags": [{"name": "Tag"}],
            "folder": {"path": base}, "files": [],
        }
        self.mock_stash.find_galleries.return_value = {"count": 1, "galleries": [gallery]}
        remote_dir = "/stash-browse/gallery/1/"
        display = build_gallery_browse_display_name(gallery, ".jpg")
        album_names = [display, "b.jpg", "c.jpg"]
        self.mock_td.upload_album.return_value = self._album_result(album_names, remote_dir)
        self.mock_td.list_dir.return_value = self._ls_result(remote_dir, album_names)

        report = self._engine(media_upload_mode="browse").run_backup()

        args, kwargs = self.mock_td.upload_album.call_args
        self.assertEqual(args[1], remote_dir)
        self.assertEqual(kwargs["as_kind"], "photo")
        # the first member is a staged copy carrying the gallery display name
        first = args[0][0]
        self.assertNotEqual(first, os.path.join(base, "a.jpg"))
        self.assertEqual(os.path.basename(first), display)
        self.assertIn("Trip ∕ 2024", display)
        self.assertIn("#Studio", display)
        # the other members publish under their original names
        self.assertEqual([os.path.basename(p) for p in args[0][1:]], ["b.jpg", "c.jpg"])
        self.assertEqual(report["galleries_published"], 1)
        for image_id, name in zip((1, 2, 3), album_names):
            row = self.ledger.get(f"browse:image:{image_id}")
            self.assertEqual(row["status"], "published")
            self.assertEqual(row["remote_path"], f"{remote_dir}{name}")

    def test_browse_gallery_mixing_rule_splits_non_images_as_documents(self):
        base = os.path.join(self.temp_dir, "gals", "mixed")
        photo_a = write_file(os.path.join(base, "a.jpg"))
        photo_b = write_file(os.path.join(base, "b.jpg"))
        clip = write_file(os.path.join(base, "clip.mp4"), b"clip-bytes")
        members = [
            make_image(1, photo_a, galleries=[1]),
            make_image(2, photo_b, galleries=[1]),
            make_image(3, clip, galleries=[1]),
        ]
        self.mock_stash.find_images.return_value = {"count": 3, "images": members}
        self.mock_stash.find_galleries.return_value = {
            "count": 1,
            "galleries": [{"id": "1", "title": "Mixed", "image_count": 3,
                            "folder": {"path": base}, "files": []}],
        }
        display = build_gallery_browse_display_name(
            {"id": "1", "title": "Mixed", "image_count": 3}, ".jpg"
        )
        self.mock_td.upload_album.return_value = self._album_result([display, "b.jpg"], "/stash-browse/gallery/1/")
        self.mock_td.list_dir.return_value = self._ls_result("/stash-browse/gallery/1/", [display, "b.jpg"])
        self.mock_td.upload_file.return_value = {"message_id": 9, "hash": "blake3:d"}

        report = self._engine(media_upload_mode="browse").run_backup()

        # the photos form the album; the clip cannot share their media group
        album_args, _ = self.mock_td.upload_album.call_args
        self.assertEqual([os.path.basename(p) for p in album_args[0]], [display, "b.jpg"])
        doc_kwargs = self.mock_td.upload_file.call_args.kwargs
        self.assertEqual(doc_kwargs["as_kind"], None)
        self.assertEqual(doc_kwargs["remote_path"], "/stash-browse/gallery/1/clip.mp4")
        self.assertEqual(report["galleries_published"], 1)

    def test_browse_zip_gallery_stages_members_from_stash_http(self):
        zip_path = os.path.join(self.temp_dir, "gals", "packed.zip")
        # zip members have no disk files; bytes must come from Stash's image endpoint
        members = [
            make_image(7, os.path.join(zip_path, "01.jpg"), galleries=[3]),
            make_image(8, os.path.join(zip_path, "02.jpg"), galleries=[3]),
        ]
        self.mock_stash.find_images.return_value = {"count": 2, "images": members}
        self.mock_stash.find_galleries.return_value = {
            "count": 1,
            "galleries": [{"id": "3", "title": "Packed", "image_count": 2,
                            "files": [{"id": 55, "path": zip_path, "size": 9}]}],
        }

        def fake_download(url, dest, **kwargs):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as handle:
                handle.write(IMAGE_BYTES)
            return True

        self.mock_stash.download_image.side_effect = fake_download
        remote_dir = "/stash-browse/gallery/3/"
        self.mock_td.upload_album.return_value = self._album_result(["7.jpg", "8.jpg"], remote_dir)
        self.mock_td.list_dir.return_value = self._ls_result(remote_dir, ["7.jpg", "8.jpg"])

        report = self._engine(media_upload_mode="browse").run_backup()

        # each member was fetched from Stash once and staged by image id
        self.assertEqual(self.mock_stash.download_image.call_count, 2)
        args, kwargs = self.mock_td.upload_album.call_args
        self.assertEqual(args[1], remote_dir)
        self.assertEqual(kwargs["as_kind"], "photo")
        # the zip itself is not uploaded in browse mode
        self.assertEqual(self.mock_td.upload_file.call_count, 0)
        self.assertEqual(report["galleries_published"], 1)
        for image_id in (7, 8):
            self.assertEqual(self.ledger.get(f"browse:image:{image_id}")["status"], "published")

    # ----------------------------------------------------------- incremental

    def test_published_gallery_members_not_reuploaded(self):
        base = os.path.join(self.temp_dir, "gals", "trip")
        names = ["a.jpg", "b.jpg"]
        for name in names:
            write_file(os.path.join(base, name))
        members = [make_image(i + 1, os.path.join(base, name), galleries=[1]) for i, name in enumerate(names)]
        self.mock_stash.find_images.return_value = {"count": 2, "images": members}
        self.mock_stash.find_galleries.return_value = {
            "count": 1,
            "galleries": [{"id": "1", "title": "Trip", "image_count": 2,
                            "folder": {"path": base}, "files": []}],
        }
        engine = self._engine()
        self.mock_td.upload_album.return_value = self._album_result(names, base + "/")
        self.mock_td.list_dir.return_value = self._ls_result(base + "/", names)

        first = engine.run_backup()
        self.assertEqual(first["galleries_published"], 1)
        self.assertEqual(self.mock_td.upload_album.call_count, 1)

        second = engine.run_backup()
        # a changed gallery must plan around ERR_USAGE on --replace: members
        # already published are simply not part of any new invocation
        self.assertEqual(self.mock_td.upload_album.call_count, 1)
        self.assertEqual(second["galleries_published"], 1)
        self.assertEqual(second["coverage_objects_percent"], 100.0)

    def test_empty_folder_gallery_reported_as_skipped(self):
        self.mock_stash.find_galleries.return_value = {
            "count": 1,
            "galleries": [{"id": "9", "title": "Ghost Gallery", "image_count": 0,
                            "folder": {"path": "/gone"}, "files": []}],
        }
        report = self._engine().run_backup()
        self.assertEqual(report["galleries_skipped"], 1)
        self.assertIn("no images", report["skipped_details"][0]["reason"])

    def test_toggles_gate_the_gallery_leg(self):
        engine = self._engine(backup_galleries=False)
        engine.run_backup()
        self.mock_stash.find_galleries.assert_not_called()
        self.mock_stash.find_images.assert_not_called()


class TestStandaloneImageBackup(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_ledger.sqlite")
        self.ledger = Ledger(self.db_path)

        self.mock_stash = MagicMock()
        self.mock_td = MagicMock()
        self.mock_stash.find_images.return_value = {"count": 0, "images": []}
        self.mock_stash.find_galleries.return_value = {"count": 0, "galleries": []}
        self.settings = base_settings(
            backup_scenes=False, backup_galleries=False,
            backup_metadata=False, backup_database=False, backup_config=False,
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _engine(self, **setting_overrides):
        settings = dict(self.settings)
        settings.update(setting_overrides)
        return SyncEngine(
            stash=self.mock_stash, td=self.mock_td,
            ledger=self.ledger, settings=settings, dry_run=False,
        )

    def test_archive_image_publishes_document_mirroring_library(self):
        path = write_file(os.path.join(self.temp_dir, "pics", "poster.jpg"))
        image = make_image(42, path, size=2048, title="Poster")
        self.mock_stash.find_images.return_value = {"count": 1, "images": [image]}
        self.mock_td.upload_file.return_value = {"message_id": 77, "hash": "blake3:p"}

        report = self._engine().run_backup()

        kwargs = self.mock_td.upload_file.call_args.kwargs
        self.assertEqual(kwargs["as_kind"], "document")
        self.assertEqual(kwargs["remote_path"], path)
        row = self.ledger.get(path)
        self.assertEqual(row["kind"], "image:archive")
        self.assertEqual(row["remote_path"], path)
        self.assertEqual(report["images_published"], 1)
        self.assertEqual(report["images_total"], 1)

    def test_scene_batch_limit_does_not_truncate_image_library(self):
        paths = [write_file(os.path.join(self.temp_dir, f"{i}.jpg")) for i in (1, 2)]
        self.mock_stash.find_images.return_value = {
            "count": 2, "images": [make_image(i + 1, p) for i, p in enumerate(paths)],
        }
        self.mock_td.upload_file.return_value = {"message_id": 77, "hash": "blake3:p"}

        report = self._engine(batch_size=1).run_backup()

        self.assertEqual(report["images_published"], 2)
        self.assertEqual(self.mock_td.upload_file.call_count, 2)
        for path in paths:
            self.assertEqual(self.ledger.get(path)["status"], "published")

    def test_browse_image_publishes_native_photo(self):
        path = write_file(os.path.join(self.temp_dir, "pics", "poster.jpg"))
        image = make_image(
            42, path, size=2048, title="Poster", code="P-1", date="2025-01-02",
            studio={"name": "Studio"}, performers=[{"name": "Performer"}], tags=[{"name": "Tag"}],
        )
        self.mock_stash.find_images.return_value = {"count": 1, "images": [image]}
        self.mock_td.upload_file.return_value = {"message_id": 78, "hash": "blake3:q"}

        report = self._engine(media_upload_mode="browse").run_backup()

        kwargs = self.mock_td.upload_file.call_args.kwargs
        self.assertEqual(kwargs["as_kind"], "photo")
        self.assertTrue(kwargs["remote_path"].startswith("/stash-browse/image/42/"))
        self.assertIn("#Studio", kwargs["remote_path"])
        self.assertIn("#Tag", kwargs["remote_path"])
        self.assertTrue(kwargs["remote_path"].endswith(".jpg"))
        row = self.ledger.get("browse:image:42")
        self.assertEqual(row["kind"], "image:browse")
        self.assertEqual(row["remote_path"], kwargs["remote_path"])
        self.assertEqual(report["images_published"], 1)

    def test_browse_video_clip_image_publishes_as_video(self):
        path = write_file(os.path.join(self.temp_dir, "pics", "clip.mp4"), b"clip-bytes")
        image = make_image(43, path, size=4096)
        image["visual_files"][0]["duration"] = 6.0
        self.mock_stash.find_images.return_value = {"count": 1, "images": [image]}
        self.mock_td.upload_file.return_value = {"message_id": 79, "hash": "blake3:v"}

        report = self._engine(media_upload_mode="browse").run_backup()

        kwargs = self.mock_td.upload_file.call_args.kwargs
        self.assertEqual(kwargs["as_kind"], "video")
        self.assertTrue(kwargs["streaming"])
        self.assertEqual(kwargs["duration"], 6.0)
        self.assertEqual(report["images_published"], 1)

    def test_missing_image_file_reported_not_uploaded(self):
        image = make_image(44, "/data/gone.jpg", size=10)
        self.mock_stash.find_images.return_value = {"count": 1, "images": [image]}

        report = self._engine().run_backup()

        self.mock_td.upload_file.assert_not_called()
        self.assertEqual(report["images_skipped"], 1)
        self.assertIn("not found on disk", report["skipped_details"][0]["reason"])
        self.assertEqual(self.ledger.get("/data/gone.jpg")["status"], "skipped")

    def test_dry_run_reports_without_uploads_or_ledger(self):
        path = write_file(os.path.join(self.temp_dir, "pics", "poster.jpg"))
        image = make_image(42, path, size=2048)
        self.mock_stash.find_images.return_value = {"count": 1, "images": [image]}

        engine = SyncEngine(
            stash=self.mock_stash, td=self.mock_td, ledger=self.ledger,
            settings=dict(self.settings), dry_run=True,
        )
        report = engine.run_backup()

        self.mock_td.upload_file.assert_not_called()
        self.mock_stash.export_objects.assert_not_called()
        self.assertEqual(report["images_published"], 1)
        self.assertEqual(report["coverage_bytes_percent"], 100.0)
        self.assertIsNone(self.ledger.get(path))


class TestSceneSelection(unittest.TestCase):
    """scene_selection=curated uploads only identified (title+tags+stash-box match) scenes."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_ledger.sqlite")
        self.ledger = Ledger(self.db_path)
        self.mock_stash = MagicMock()
        self.mock_td = MagicMock()
        self.mock_stash.find_images.return_value = {"count": 0, "images": []}
        self.mock_stash.find_galleries.return_value = {"count": 0, "galleries": []}
        self.mock_stash.download_image.return_value = False
        self.mock_td.upload_file.return_value = {"message_id": 7, "hash": "blake3:q"}
        self.settings = base_settings(
            backup_metadata=False, backup_database=False, backup_config=False
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _scenes(self):
        curated = {
            "id": "10", "title": "Example Scene", "code": None, "date": "2021-01-27",
            "studio": {"name": "Example Studio"},
            "performers": [{"name": "Example Performer"}],
            "tags": [{"name": "Example Tag"}],
            "paths": {"screenshot": None},
            "stash_ids": [{"endpoint": "https://stashdb.org/graphql", "stash_id": "abc"}],
            "files": [{"id": 10, "path": "/data/curated.mp4", "size": 100, "duration": 60}],
        }
        untagged = {
            "id": "11", "title": "No Tags Yet", "code": None, "date": None,
            "studio": None, "performers": [], "tags": [],
            "paths": {"screenshot": None},
            "stash_ids": [{"endpoint": "https://stashdb.org/graphql", "stash_id": "def"}],
            "files": [{"id": 11, "path": "/data/untagged.mp4", "size": 100, "duration": 60}],
        }
        unmatched = {
            "id": "12", "title": "Unmatched", "code": None, "date": None,
            "studio": None, "performers": [], "tags": [{"name": "Movie Tag"}],
            "paths": {"screenshot": None},
            "stash_ids": [],
            "files": [{"id": 12, "path": "/data/unmatched.mp4", "size": 100, "duration": 60}],
        }
        return [curated, untagged, unmatched]

    def test_curated_uploads_only_identified_scenes(self):
        self.mock_stash.find_scenes.return_value = {"count": 3, "scenes": self._scenes()}
        engine = SyncEngine(
            stash=self.mock_stash, td=self.mock_td, ledger=self.ledger,
            settings={**self.settings, "scene_selection": "curated"},
            dry_run=False,
        )
        report = engine.run_backup()

        self.assertEqual(report["scenes_total"], 3)
        self.assertEqual(report["scenes_published"], 1)
        self.assertEqual(report["scenes_skipped"], 2)
        self.assertEqual(len(report["skipped_details"]), 2)
        self.assertEqual(
            {d["scene_id"] for d in report["skipped_details"]}, {"11", "12"}
        )
        self.assertIn("curated", report["skipped_details"][0]["reason"])
        self.assertEqual(self.mock_td.upload_file.call_count, 1)
        self.assertEqual(self.mock_td.upload_file.call_args.kwargs["local_path"], "/data/curated.mp4")
        self.assertEqual(self.ledger.get("/data/curated.mp4")["status"], "published")
        self.assertEqual(self.ledger.get("/data/untagged.mp4")["status"], "skipped")
        self.assertEqual(self.ledger.get("/data/unmatched.mp4")["status"], "skipped")

    def test_all_selection_uploads_every_scene(self):
        self.mock_stash.find_scenes.return_value = {"count": 3, "scenes": self._scenes()}
        engine = SyncEngine(
            stash=self.mock_stash, td=self.mock_td, ledger=self.ledger,
            settings={**self.settings, "scene_selection": "all"},
            dry_run=False,
        )
        report = engine.run_backup()

        self.assertEqual(report["scenes_published"], 3)
        self.assertEqual(report["scenes_skipped"], 0)
        self.assertEqual(self.mock_td.upload_file.call_count, 3)


class TestContentChange(unittest.TestCase):
    """P3: changed bytes at an unchanged path are re-published (cp --replace)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_ledger.sqlite")
        self.ledger = Ledger(self.db_path)
        self.mock_stash = MagicMock()
        self.mock_td = MagicMock()
        self.mock_stash.find_scenes.return_value = {"count": 0, "scenes": []}
        self.mock_stash.find_images.return_value = {"count": 0, "images": []}
        self.mock_stash.find_galleries.return_value = {"count": 0, "galleries": []}
        self.settings = base_settings(
            backup_metadata=False, backup_database=False, backup_config=False
        )

    def tearDown(self):
        try:
            import shutil
            shutil.rmtree(self.temp_dir)
        except Exception:
            pass

    def _engine(self, **overrides):
        settings = dict(self.settings)
        settings.update(overrides)
        return SyncEngine(
            stash=self.mock_stash, td=self.mock_td, ledger=self.ledger,
            settings=settings, dry_run=False,
        )

    def test_scene_changed_content_republishes_with_replace(self):
        # ledger has the scene published with the OLD fingerprint; Stash now
        # reports NEW bytes (fingerprint differs, size differs)
        scene_path = os.path.join(self.temp_dir, "videos", "scene.mp4")
        self.ledger.record_success(
            scene_path, "scene:archive", 100,
            blake3="blake3:old", message_id=1, remote_path=scene_path,
            fingerprint="md5=old",
        )
        self.mock_stash.find_scenes.return_value = {
            "count": 1,
            "scenes": [{
                "id": "1", "title": "Scene", "files": [{
                    "id": 101, "path": scene_path, "size": 250,
                    "duration": 60, "width": 1920, "height": 1080,
                    "fingerprints": [{"type": "md5", "value": "new"}],
                }],
            }],
        }
        self.mock_td.upload_file.return_value = {"message_id": 2, "hash": "blake3:new"}

        report = self._engine().run_backup()

        self.assertEqual(report["scenes_updated"], 1)
        self.assertEqual(report["scenes_published"], 1)
        kwargs = self.mock_td.upload_file.call_args.kwargs
        self.assertTrue(kwargs["replace"])
        self.assertEqual(kwargs["remote_path"], scene_path)
        row = self.ledger.get(scene_path)
        self.assertEqual(row["blake3"], "blake3:new")
        self.assertEqual(row["fingerprint"], "md5=new")

    def test_scene_unchanged_content_not_republished(self):
        scene_path = os.path.join(self.temp_dir, "videos", "scene.mp4")
        self.ledger.record_success(
            scene_path, "scene:archive", 250,
            blake3="blake3:new", message_id=1, remote_path=scene_path,
            fingerprint="md5=new",
        )
        self.mock_stash.find_scenes.return_value = {
            "count": 1,
            "scenes": [{
                "id": "1", "title": "Renamed Scene", "files": [{
                    "id": 101, "path": scene_path, "size": 250,
                    "duration": 60, "width": 1920, "height": 1080,
                    "fingerprints": [{"type": "md5", "value": "new"}],
                }],
            }],
        }

        report = self._engine().run_backup()

        self.assertEqual(report["scenes_updated"], 0)
        self.assertEqual(report["scenes_published"], 1)
        self.mock_td.upload_file.assert_not_called()

    def test_standalone_image_changed_republishes_with_replace(self):
        poster = write_file(os.path.join(self.temp_dir, "pics", "poster.jpg"))
        self.ledger.record_success(
            poster, "image:archive", 100,
            blake3="blake3:old", message_id=1, remote_path=poster,
            fingerprint="md5=old",
        )
        self.mock_stash.find_images.return_value = {
            "count": 1,
            "images": [{
                "id": "42", "title": "Poster", "code": None, "date": None,
                "studio": None, "performers": [], "tags": [],
                "visual_files": [{
                    "id": 942, "path": poster, "size": 140,
                    "width": 800, "height": 600,
                    "fingerprints": [{"type": "md5", "value": "new"}],
                }],
                "paths": {"image": "http://stash:9999/image/42/image"},
                "galleries": [],
            }],
        }
        self.mock_td.upload_file.return_value = {"message_id": 2, "hash": "blake3:y"}

        report = self._engine().run_backup()

        self.assertEqual(report["images_updated"], 1)
        self.assertEqual(report["images_published"], 1)
        kwargs = self.mock_td.upload_file.call_args.kwargs
        self.assertTrue(kwargs["replace"])
        self.assertEqual(kwargs["remote_path"], poster)

    def test_gallery_member_changed_republishes_single_document(self):
        base = os.path.join(self.temp_dir, "gals", "trip")
        path = write_file(os.path.join(base, "a.jpg"))
        # ledger says a.jpg was published with the old fingerprint
        self.ledger.record_success(
            path, "image:archive", 100,
            blake3="blake3:old", message_id=1, remote_path=path,
            fingerprint="md5=old",
        )
        # b.jpg and c.jpg are brand-new: they go into the album group
        new_members = [
            write_file(os.path.join(base, "b.jpg"), b"b-bytes"),
            write_file(os.path.join(base, "c.jpg"), b"c-bytes"),
        ]
        members = [
            make_image(1, path, galleries=[1], visual_files=[{
                "id": 901, "path": path, "size": 130,
                "width": 800, "height": 600,
                "fingerprints": [{"type": "md5", "value": "new"}],
            }]),
            make_image(2, new_members[0], galleries=[1]),
            make_image(3, new_members[1], galleries=[1]),
        ]
        self.mock_stash.find_images.return_value = {"count": 3, "images": members}
        self.mock_stash.find_galleries.return_value = {
            "count": 1,
            "galleries": [{
                "id": "1", "title": "Trip", "image_count": 3,
                "folder": {"path": base}, "files": [],
            }],
        }
        self.mock_td.upload_file.return_value = {"message_id": 9, "hash": "blake3:z"}
        names = ["b.jpg", "c.jpg"]
        self.mock_td.upload_album.return_value = self._album_result(names, base + "/", start=20)
        self.mock_td.list_dir.return_value = [
            {"type": "file", "name": name, "path": f"{base}/{name}",
             "size": 100, "hash": f"blake3:abc{i}"}
            for i, name in enumerate(names)
        ]

        report = self._engine().run_backup()

        self.assertEqual(report["images_updated"], 1)
        self.assertEqual(report["galleries_published"], 1)
        # a.jpg re-published via rm + fresh single cp (albums cannot
        # --replace), b/c grouped into the album
        self.mock_td.remove.assert_called_once_with(path)
        self.assertEqual(self.mock_td.upload_file.call_count, 1)
        call_kwargs = self.mock_td.upload_file.call_args.kwargs
        self.assertFalse(call_kwargs["replace"])
        self.assertEqual(call_kwargs["local_path"], path)
        self.assertEqual(call_kwargs["remote_path"], path)
        self.assertEqual(self.mock_td.upload_album.call_count, 1)
        # the re-published member carries the new fingerprint in the ledger
        row = self.ledger.get(path)
        self.assertEqual(row["fingerprint"], "md5=new")
        self.assertEqual(row["message_id"], 9)

    def test_zip_gallery_changed_republishes_zip(self):
        zip_path = write_file(os.path.join(self.temp_dir, "gals", "packed.zip"), b"new-zippy")
        self.ledger.record_success(
            zip_path, "gallery:archive", 9,
            blake3="blake3:old", message_id=1, remote_path=zip_path,
            fingerprint="md5=old",
        )
        self.mock_stash.find_galleries.return_value = {
            "count": 1,
            "galleries": [{
                "id": "3", "title": "Packed", "image_count": 2,
                "files": [{
                    "id": 55, "path": zip_path, "size": len(b"new-zippy"),
                    "fingerprints": [{"type": "md5", "value": "new"}],
                }],
            }],
        }
        self.mock_td.upload_file.return_value = {"message_id": 4, "hash": "blake3:n"}

        report = self._engine().run_backup()

        self.assertEqual(report["galleries_updated"], 1)
        self.assertEqual(report["galleries_published"], 1)
        kwargs = self.mock_td.upload_file.call_args.kwargs
        self.assertTrue(kwargs["replace"])
        self.assertEqual(kwargs["local_path"], zip_path)

    def _album_result(self, names, prefix, start=11):
        return {
            "uploaded": len(names), "skipped": 0, "errors": [],
            "albums": [{
                "grouped_id": 7001, "reply_message_id": 99,
                "message_ids": [start + i for i in range(len(names))],
                "paths": [f"{prefix}{name}" for name in names],
            }],
            "channel_id": "-100123",
        }


if __name__ == "__main__":
    unittest.main()
