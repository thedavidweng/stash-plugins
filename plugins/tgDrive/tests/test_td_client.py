import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from td_client import TDClient, TDRateLimitedError, TDLockedError, TDNotFoundError, TDError


class TestTDClient(unittest.TestCase):
    def setUp(self):
        self.client = TDClient(td_path="/mock/td", data_dir="/td-data")

    def _mock_run(self, data, returncode=0):
        return MagicMock(
            returncode=returncode,
            stdout=json.dumps({"ok": True, "data": data}),
            stderr="",
        )

    @patch("subprocess.run")
    def test_upload_success(self, mock_run):
        mock_run.return_value = self._mock_run({
            "path": "/data/test.mp4",
            "message_id": 999,
            "hash": "blake3:deadbeef",
            "size": 12345
        })

        res = self.client.upload_file(
            local_path="/local/test.mp4",
            remote_path="/data/test.mp4",
            as_kind="video",
            duration=120.5,
            width=1920,
            height=1080,
            streaming=True,
        )

        self.assertEqual(res["message_id"], 999)
        self.assertEqual(res["hash"], "blake3:deadbeef")
        cmd = mock_run.call_args.args[0]
        self.assertEqual(cmd[:2], ["/mock/td", "cp"])
        self.assertIn("--as", cmd)
        self.assertIn("video", cmd)
        self.assertIn("--streaming", cmd)

    @patch("subprocess.run")
    def test_upload_replace_requires_confirm(self, mock_run):
        mock_run.return_value = self._mock_run({"path": "/data/test.mp4", "message_id": 1})
        self.client.upload_file("/a", "/b", replace=True)
        cmd = mock_run.call_args.args[0]
        self.assertIn("--replace", cmd)
        self.assertIn("--confirm", cmd)

    @patch("subprocess.run")
    def test_upload_waits_through_flood_waits(self, mock_run):
        """Uploads pass --wait so td retries FLOOD_WAITs in-process."""
        mock_run.return_value = self._mock_run({"path": "/b", "message_id": 1})
        self.client.upload_file("/a", "/b")
        cmd = mock_run.call_args.args[0]
        self.assertIn("--wait", cmd)

    @patch("subprocess.run")
    def test_download_uses_get_not_cp(self, mock_run):
        """Regression: downloads must use `td get` (td cp is upload-only)."""
        mock_run.return_value = self._mock_run({"downloaded": 3, "skipped": 0, "failed": 0})

        self.client.download("/data", "/restore/data", recursive=True, skip_existing=True, continue_on_error=True)

        cmd = mock_run.call_args.args[0]
        self.assertEqual(cmd[1], "get")
        self.assertNotIn("cp", cmd[:2])
        self.assertEqual(cmd[cmd.index("/data"):cmd.index("/data") + 2], ["/data", "/restore/data"])
        self.assertIn("--recursive", cmd)
        self.assertIn("--skip-existing", cmd)
        self.assertIn("--continue-on-error", cmd)

    @patch("subprocess.run")
    def test_rate_limited_error(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout=json.dumps({
                "ok": False,
                "error": {
                    "code": "ERR_TELEGRAM_RATE_LIMITED",
                    "message": "Flood wait",
                    "details": {
                        "retry_after_seconds": 45
                    }
                }
            }),
            stderr="",
        )

        with self.assertRaises(TDRateLimitedError) as ctx:
            self.client.upload_file("/a", "/b")
        self.assertEqual(ctx.exception.retry_after_seconds, 45)

    @patch("subprocess.run")
    def test_locked_error(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout=json.dumps({
                "ok": False,
                "error": {
                    "code": "ERR_OPERATION_LOCKED",
                    "message": "Operation locked"
                }
            }),
            stderr="",
        )

        with self.assertRaises(TDLockedError):
            self.client.upload_file("/a", "/b")

    @patch("subprocess.run")
    def test_not_found_error(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout=json.dumps({
                "ok": False,
                "error": {
                    "code": "ERR_REMOTE_NOT_FOUND",
                    "message": "remote path not found"
                }
            }),
            stderr="",
        )

        with self.assertRaises(TDNotFoundError):
            self.client.download("/gone.zip", "/tmp/x.zip")

    @patch("subprocess.run")
    def test_list_dir_parses_contract_envelope(self, mock_run):
        """td ls --json data is {path, entries: [...]}, not a bare array."""
        mock_run.return_value = self._mock_run({
            "path": "/",
            "entries": [
                {"type": "dir", "name": "volume1", "path": "/volume1"},
                {"type": "file", "name": "loose.mp4", "path": "/loose.mp4",
                 "size": 5, "hash": "blake3:abc"},
            ],
        })
        entries = self.client.list_dir("/")
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["name"], "volume1")
        self.assertEqual(entries[1]["hash"], "blake3:abc")

    @patch("subprocess.run")
    def test_data_directory_is_passed_to_td(self, mock_run):
        mock_run.return_value = self._mock_run({"authenticated": False})

        self.client.auth_status()

        cmd = mock_run.call_args.args[0]
        self.assertEqual(cmd[:8], [
            "/mock/td", "auth", "--config", "/td-data/config.toml",
            "--session", "/td-data/session.json", "--db", "/td-data/local_cache.db",
        ])
        self.assertEqual(cmd[-1], "--json")

    def test_command_line_matches_invocation_shape(self):
        """User-facing instructions must equal the executed command (minus --json)."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = self._mock_run({"authenticated": False})
            self.client.auth_status()
            executed = mock_run.call_args.args[0]

        displayed = self.client.command_line("auth", "status")
        self.assertEqual(
            displayed,
            "/mock/td auth --config /td-data/config.toml "
            "--session /td-data/session.json --db /td-data/local_cache.db status",
        )
        self.assertEqual(displayed.split() + ["--json"], executed)

    def test_command_line_quotes_paths(self):
        client = TDClient(td_path="/opt/my td", data_dir="/data dir/td-data")
        self.assertEqual(
            client.command_line("auth", "status"),
            "'/opt/my td' auth --config '/data dir/td-data/config.toml' "
            "--session '/data dir/td-data/session.json' "
            "--db '/data dir/td-data/local_cache.db' status",
        )


if __name__ == "__main__":
    unittest.main()
