import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from restore_engine import RestoreEngine
from td_client import TDError, TDNotFoundError


def remote_entries():
    return [
        {"name": "volume1", "path": "/volume1", "type": "dir"},
        {"name": "loose.mp4", "path": "/loose.mp4", "type": "file"},
        {"name": "stash-metadata", "path": "/stash-metadata", "type": "dir"},
        {"name": "stash-backup", "path": "/stash-backup", "type": "dir"},
    ]


class TestRestoreEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.restore_dir = os.path.join(self.temp_dir, "restore")

        self.mock_stash = MagicMock()
        self.mock_td = MagicMock()

        self.mock_td.scan.return_value = {"active": 42}
        self.mock_td.list_dir.return_value = remote_entries()
        self.mock_td.download.return_value = {"downloaded": 1, "skipped": 0, "failed": 0}

        self.mock_stash.trigger_scan.return_value = "scan-job"
        self.mock_stash.wait_for_job.return_value = "FINISHED"
        self.mock_stash.import_objects.return_value = "import-job"

    def tearDown(self):
        try:
            import shutil
            shutil.rmtree(self.temp_dir)
        except Exception:
            pass

    def _engine(self):
        return RestoreEngine(
            stash=self.mock_stash,
            td=self.mock_td,
            restore_dir=self.restore_dir,
            job_timeout_seconds=60,
        )

    def _mark_export_present(self):
        def dl(remote, local, **kwargs):
            os.makedirs(os.path.dirname(os.path.abspath(local)), exist_ok=True)
            if remote.endswith(".zip") or remote.endswith(".yml"):
                with open(local, "wb") as f:
                    f.write(b"export-zip")
            return {"downloaded": 1}
        self.mock_td.download.side_effect = dl

    def test_restore_downloads_media_roots_and_skips_service_dirs(self):
        self.mock_td.download.side_effect = TDNotFoundError("remote path not found")
        self._engine().run_restore()

        download_calls = [c for c in self.mock_td.download.call_args_list]
        media_remotes = [
            c.args[0] for c in download_calls
            if c.args[0] in ("/volume1", "/loose.mp4")
        ]
        self.assertEqual(sorted(media_remotes), ["/loose.mp4", "/volume1"])

        # service roots are never downloaded as media
        for c in download_calls:
            self.assertNotEqual(c.args[0], "/stash-metadata")
            self.assertNotEqual(c.args[0], "/stash-backup")

        # dir roots recurse into a local mirror of the remote name
        dir_call = [c for c in download_calls if c.args[0] == "/volume1"][0]
        self.assertEqual(dir_call.args[1], os.path.join(self.restore_dir, "volume1"))
        self.assertTrue(dir_call.kwargs.get("recursive"))
        self.assertTrue(dir_call.kwargs.get("skip_existing"))
        self.assertTrue(dir_call.kwargs.get("continue_on_error"))

        # top-level files restore as exact paths
        file_call = [c for c in download_calls if c.args[0] == "/loose.mp4"][0]
        self.assertEqual(file_call.args[1], os.path.join(self.restore_dir, "loose.mp4"))
        self.assertFalse(file_call.kwargs.get("recursive", False))

    def test_recovery_artifacts_go_to_stash_recovery(self):
        self._mark_export_present()

        report = self._engine().run_restore()
        recovery_dir = os.path.join(self.restore_dir, "stash-recovery")

        remote_dests = {c.args[0]: c.args[1] for c in self.mock_td.download.call_args_list}
        self.assertIn("/stash-backup/database/stash-backup.zip", remote_dests)
        self.assertIn("/stash-backup/config/config.yml", remote_dests)
        self.assertEqual(
            remote_dests["/stash-backup/database/stash-backup.zip"],
            os.path.join(recovery_dir, "database", "stash-backup.zip"),
        )
        self.assertEqual(
            remote_dests["/stash-backup/config/config.yml"],
            os.path.join(recovery_dir, "config", "config.yml"),
        )
        self.assertEqual(report["recovery_artifacts"]["database"]["status"], "ok")
        self.assertEqual(report["recovery_artifacts"]["config"]["status"], "ok")

    def test_scan_then_import_order(self):
        self._mark_export_present()
        # media roots must exist on disk to be scanned
        os.makedirs(os.path.join(self.restore_dir, "volume1"), exist_ok=True)

        report = self._engine().run_restore()

        # scan receives the local media roots
        self.mock_stash.trigger_scan.assert_called_once_with(
            [os.path.join(self.restore_dir, "volume1")]
        )
        # import happens after scan finished, with duplicate=OVERWRITE / missing=CREATE
        self.mock_stash.import_objects.assert_called_once()
        args, kwargs = self.mock_stash.import_objects.call_args
        self.assertEqual(args[0].endswith("stash-export.zip"), True)
        self.assertEqual(kwargs.get("duplicate_behaviour"), "OVERWRITE")
        self.assertEqual(kwargs.get("missing_ref_behaviour"), "CREATE")

        self.assertEqual(report["stash_scan"]["status"], "FINISHED")
        self.assertEqual(report["metadata_import"]["status"], "FINISHED")

        # the scan job and the import job were both awaited
        waited_jobs = [c.args[0] for c in self.mock_stash.wait_for_job.call_args_list]
        self.assertEqual(waited_jobs, ["scan-job", "import-job"])

    def test_import_skipped_when_scan_did_not_finish(self):
        self._mark_export_present()
        self.mock_stash.wait_for_job.side_effect = ["FAILED", "FAILED"]

        report = self._engine().run_restore()

        self.mock_stash.import_objects.assert_not_called()
        self.assertIn("skipped", report["metadata_import"]["status"])

    def test_absent_artifacts_reported_as_absent(self):
        self.mock_td.download.side_effect = TDNotFoundError("remote path not found")

        report = self._engine().run_restore()

        self.assertEqual(report["recovery_artifacts"]["database"]["status"], "absent")
        self.assertEqual(report["recovery_artifacts"]["config"]["status"], "absent")
        self.assertEqual(report["recovery_artifacts"]["export"]["status"], "absent")
        self.mock_stash.import_objects.assert_not_called()
        self.assertIn("absent", report["metadata_import"]["status"])

    def test_download_error_recorded(self):
        self.mock_td.download.side_effect = TDError("boom", code="ERR_TELEGRAM_RPC")

        report = self._engine().run_restore()

        self.assertEqual(report["recovery_artifacts"]["database"]["status"], "failed")
        self.assertIn("boom", report["recovery_artifacts"]["database"]["error"])


if __name__ == "__main__":
    unittest.main()
