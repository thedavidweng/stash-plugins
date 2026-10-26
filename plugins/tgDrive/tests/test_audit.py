import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audit import backfill_adopted_library, run_deletion_check, run_remote_audit
from ledger import Ledger
from td_client import TDError, TDNotFoundError


def remote_tree():
    return [
        {"type": "dir", "name": "data", "path": "/data", "children": [
            {"type": "file", "name": "scene1.mp4", "path": "/data/scene1.mp4"},
            {"type": "file", "name": "scene2.mp4", "path": "/data/scene2.mp4"},
            {"type": "dir", "name": "gals", "path": "/data/gals", "children": [
                {"type": "file", "name": "a.jpg", "path": "/data/gals/a.jpg"},
                {"type": "file", "name": "b.jpg", "path": "/data/gals/b.jpg"},
            ]},
        ]},
        {"type": "dir", "name": "stash-browse", "path": "/stash-browse", "children": [
            {"type": "file", "name": "9", "path": "/stash-browse/9", "children": []},
        ]},
        {"type": "dir", "name": "stash-metadata", "path": "/stash-metadata", "children": [
            {"type": "file", "name": "export.zip", "path": "/stash-metadata/export.zip"},
        ]},
    ]


def ls_files(entries):
    return [{"type": "file", "name": os.path.basename(p), "path": p, "size": s, "hash": h}
            for p, s, h in entries]


class TestRemoteAudit(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.ledger = Ledger(os.path.join(self.temp_dir, "ledger.sqlite"))
        self.mock_td = MagicMock()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _publish(self, path, size=100, blake3="blake3:aa", remote_path=None, kind="scene:archive"):
        self.ledger.record_success(
            path, kind, size, blake3=blake3, message_id=1,
            remote_path=remote_path,
        )

    def test_full_agreement_reports_ok(self):
        self._publish("/data/scene1.mp4")
        self._publish("/data/gals/a.jpg", kind="image:archive")
        self._publish(
            "browse:/data/scene2.mp4", kind="scene:browse",
            remote_path="/stash-browse/9/scene-name.mp4",
        )
        self.mock_td.tree.return_value = [
            {"type": "dir", "name": "data", "path": "/data", "children": [
                {"type": "file", "name": "scene1.mp4", "path": "/data/scene1.mp4"},
                {"type": "dir", "name": "gals", "path": "/data/gals", "children": [
                    {"type": "file", "name": "a.jpg", "path": "/data/gals/a.jpg"},
                ]},
            ]},
            {"type": "dir", "name": "stash-browse", "path": "/stash-browse", "children": [
                {"type": "dir", "name": "9", "path": "/stash-browse/9", "children": [
                    {"type": "file", "name": "scene-name.mp4", "path": "/stash-browse/9/scene-name.mp4"},
                ]},
            ]},
        ]
        self.mock_td.list_dir.side_effect = [
            ls_files([("/data/scene1.mp4", 100, "blake3:aa")]),
            ls_files([("/data/gals/a.jpg", 100, "blake3:aa")]),
            ls_files([("/stash-browse/9/scene-name.mp4", 100, "blake3:aa")]),
        ]

        report = run_remote_audit(self.mock_td, self.ledger)

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["verified"], 3)
        self.assertEqual(report["remote_files"], 3)
        self.assertEqual(report["remote_extras"]["count"], 0)
        self.assertEqual(report["missing_remote"]["count"], 0)

    def test_missing_remote_and_hash_mismatch_are_drift(self):
        self._publish("/data/scene1.mp4", blake3="blake3:good")
        self._publish("/data/gone.mp4", blake3="blake3:bb")
        self.mock_td.tree.return_value = [
            {"type": "file", "name": "scene1.mp4", "path": "/data/scene1.mp4"},
        ]
        # /data does not exist as a directory in the tree; ls of "/data" still
        # lists files directly under it (ls is per-directory, tree is flattened)
        self.mock_td.list_dir.return_value = ls_files([("/data/scene1.mp4", 100, "blake3:bad")])

        report = run_remote_audit(self.mock_td, self.ledger)

        self.assertEqual(report["status"], "drift")
        self.assertEqual(report["hash_mismatch"]["count"], 1)
        self.assertIn("/data/scene1.mp4", report["hash_mismatch"]["sample"])
        self.assertEqual(report["missing_remote"]["count"], 1)
        self.assertIn("/data/gone.mp4", report["missing_remote"]["sample"])
        self.assertEqual(report["verified"], 0)

    def test_remote_files_without_ledger_rows_are_extras(self):
        self._publish("/data/scene1.mp4")
        self.mock_td.tree.return_value = [
            {"type": "file", "name": "scene1.mp4", "path": "/data/scene1.mp4"},
            {"type": "file", "name": "drifter.mp4", "path": "/data/drifter.mp4"},
        ]
        self.mock_td.list_dir.return_value = ls_files([
            ("/data/scene1.mp4", 100, "blake3:aa"),
            ("/data/drifter.mp4", 50, "blake3:zz"),
        ])

        report = run_remote_audit(self.mock_td, self.ledger)

        # extras do not block the audit: they are reported, not fatal
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["remote_extras"]["count"], 1)
        self.assertIn("/data/drifter.mp4", report["remote_extras"]["sample"])
        self.assertEqual(report["verified"], 1)

    def test_unusable_td_degrades_to_local_only(self):
        self._publish("/data/scene1.mp4")
        self.mock_td.tree.side_effect = TDError("td drive root is not initialized", code="ERR_CONFIG_MISSING")

        report = run_remote_audit(self.mock_td, self.ledger)

        self.assertEqual(report["status"], "local_only")
        self.assertIn("not initialized", report["error"])

    def test_remote_rows_without_hash_reported_separately(self):
        # rows adopted without --hash carry "" remotely; they are distinct
        # from a real mismatch
        self._publish("/data/scene1.mp4")
        self.mock_td.tree.return_value = [
            {"type": "file", "name": "scene1.mp4", "path": "/data/scene1.mp4"},
        ]
        self.mock_td.list_dir.return_value = ls_files([("/data/scene1.mp4", 100, "")])

        report = run_remote_audit(self.mock_td, self.ledger)

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["remote_without_hash"]["count"], 1)
        self.assertEqual(report["verified"], 0)

    def test_legacy_browse_rows_cannot_be_located_remotely(self):
        # pre-remote_path browse rows: reported as unverifiable, not guessed
        self.ledger.record_success("browse:/data/old.mp4", "scene:browse", 100, blake3="blake3:aa")
        self.mock_td.tree.return_value = []

        report = run_remote_audit(self.mock_td, self.ledger)

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["unverifiable_legacy_paths"]["count"], 1)
        self.assertIn("browse:/data/old.mp4", report["unverifiable_legacy_paths"]["sample"])

    def test_vanished_directory_is_missing_not_error(self):
        self._publish("/data/gone/deep.mp4")
        self.mock_td.tree.return_value = []
        self.mock_td.list_dir.side_effect = TDNotFoundError("remote path not found")

        report = run_remote_audit(self.mock_td, self.ledger)

        self.assertEqual(report["status"], "drift")
        self.assertEqual(report["missing_remote"]["count"], 1)
        self.assertEqual(report["errors"], [])

    def test_ls_failures_are_partial_not_drift(self):
        self._publish("/data/scene1.mp4")
        self.mock_td.tree.return_value = [
            {"type": "file", "name": "scene1.mp4", "path": "/data/scene1.mp4"},
        ]
        self.mock_td.list_dir.side_effect = TDError("database is locked", code="ERR_OPERATION_LOCKED")

        report = run_remote_audit(self.mock_td, self.ledger)

        self.assertEqual(report["status"], "partial")
        self.assertTrue(any("locked" in e for e in report["errors"]))


class TestDeletionCheck(unittest.TestCase):
    """Report-only Stash-vs-ledger deletion reporting (Verify task)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.ledger = Ledger(os.path.join(self.temp_dir, "ledger.sqlite"))
        self.mock_stash = MagicMock()
        self.mock_stash.find_scenes.return_value = {"count": 0, "scenes": []}
        self.mock_stash.find_images.return_value = {"count": 0, "images": []}
        self.mock_stash.find_galleries.return_value = {"count": 0, "galleries": []}

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _publish(self, path, kind="scene:archive"):
        self.ledger.record_success(path, kind, 100, blake3="blake3:aa", message_id=1)

    def test_everything_current_reports_ok(self):
        self._publish("/data/scene1.mp4")
        self._publish("/data/gals/a.jpg", kind="image:archive")
        self.mock_stash.find_scenes.return_value = {
            "count": 1,
            "scenes": [{"id": "1", "files": [{"id": 1, "path": "/data/scene1.mp4"}]}],
        }
        self.mock_stash.find_images.return_value = {
            "count": 1,
            "images": [{"id": "3", "visual_files": [{"id": 2, "path": "/data/gals/a.jpg"}]}],
        }

        report = run_deletion_check(self.mock_stash, self.ledger)

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["deleted"], {})
        self.assertEqual(report["checked"], {"scene": 1, "image": 1, "gallery": 0})

    def test_removed_media_reported_per_kind(self):
        self._publish("/data/gone.mp4")
        self._publish("/data/gals/old.jpg", kind="image:archive")
        self._publish("/gals/packed.zip", kind="gallery:archive")
        # Stash now only has the scene; image and zip are gone
        self.mock_stash.find_scenes.return_value = {
            "count": 1,
            "scenes": [{"id": "1", "files": [{"id": 1, "path": "/data/gone.mp4"}]}],
        }

        report = run_deletion_check(self.mock_stash, self.ledger)

        self.assertEqual(report["status"], "deleted")
        self.assertEqual(report["deleted"]["image"]["count"], 1)
        self.assertEqual(report["deleted"]["gallery"]["count"], 1)
        self.assertNotIn("scene", report["deleted"])

    def test_browse_rows_skipped(self):
        # browse rows are skipped entirely: the archive row for the same
        # path is the deletion signal, so a browse-only row must never be
        # flagged even when the file is gone from Stash
        self.ledger.record_success(
            "browse:/data/scene1.mp4", "scene:browse", 100,
            blake3="blake3:aa", message_id=1,
            remote_path="/stash-browse/9/name.mp4",
        )

        report = run_deletion_check(self.mock_stash, self.ledger)

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["deleted"], {})
        self.assertEqual(report["checked"]["scene"], 0)

    def test_zip_member_virtual_paths_compared_against_images(self):
        # zip-gallery member rows carry virtual paths; findImages reports
        # them, so an existing member must not be flagged deleted
        self._publish("/gals/packed.zip/01.jpg", kind="image:archive")
        self.mock_stash.find_images.return_value = {
            "count": 1,
            "images": [{
                "id": "7",
                "visual_files": [{"id": 2, "path": "/gals/packed.zip/01.jpg"}],
            }],
        }

        report = run_deletion_check(self.mock_stash, self.ledger)

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["deleted"], {})

    def test_fetch_errors_are_reported_not_fatal(self):
        self._publish("/data/scene1.mp4")
        self.mock_stash.find_scenes.side_effect = RuntimeError("graphql down")

        report = run_deletion_check(self.mock_stash, self.ledger)

        self.assertIn("findScenes", report["errors"][0])
        self.assertIn("/data/scene1.mp4", report["deleted"]["scene"]["sample"])


class TestAdoptBackfill(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.ledger = Ledger(os.path.join(self.temp_dir, "ledger.sqlite"))
        self.mock_td = MagicMock()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_backfill_records_adopted_rows_with_hashes(self):
        self.mock_td.tree.return_value = [
            {"type": "dir", "name": "adopted", "path": "/adopted", "children": [
                {"type": "file", "name": "legacy.mp4", "path": "/adopted/legacy.mp4"},
            ]},
        ]
        self.mock_td.list_dir.return_value = [
            {"type": "file", "name": "legacy.mp4", "path": "/adopted/legacy.mp4",
             "size": 512, "hash": "blake3:abc"},
        ]

        result = backfill_adopted_library(self.mock_td, self.ledger, "/adopted")

        self.assertEqual(result["rows_added"], 1)
        row = self.ledger.get("/adopted/legacy.mp4")
        self.assertEqual(row["kind"], "adopted:archive")
        self.assertEqual(row["blake3"], "blake3:abc")
        self.assertEqual(row["remote_path"], "/adopted/legacy.mp4")

    def test_backfill_never_overwrites_existing_rows(self):
        self._publish("/adopted/legacy.mp4", kind="image:archive", blake3="blake3:old")
        self.mock_td.tree.return_value = [
            {"type": "dir", "name": "adopted", "path": "/adopted", "children": [
                {"type": "file", "name": "legacy.mp4", "path": "/adopted/legacy.mp4"},
            ]},
        ]
        self.mock_td.list_dir.return_value = [
            {"type": "file", "name": "legacy.mp4", "path": "/adopted/legacy.mp4",
             "size": 99, "hash": "blake3:remote"},
        ]

        result = backfill_adopted_library(self.mock_td, self.ledger, "/adopted")

        self.assertEqual(result["rows_added"], 0)
        row = self.ledger.get("/adopted/legacy.mp4")
        self.assertEqual(row["kind"], "image:archive")

    def _publish(self, path, kind="image:archive", blake3="blake3:old"):
        self.ledger.record_success(path, kind, 100, blake3=blake3, message_id=1)


if __name__ == "__main__":
    unittest.main()
