import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from td_client import TDClient, TDRateLimitedError, TDLockedError, TDNotFoundError, TDError


class TestTDClient(unittest.TestCase):
    def setUp(self):
        self.client = TDClient(td_path="/mock/td")

    @patch("subprocess.run")
    def test_upload_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "ok": True,
                "data": {
                    "path": "/data/test.mp4",
                    "message_id": 999,
                    "hash": "blake3:deadbeef",
                    "size": 12345
                }
            }),
            stderr="",
        )

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
                    "code": "ERR_LOCKED",
                    "message": "Operation locked"
                }
            }),
            stderr="",
        )

        with self.assertRaises(TDLockedError):
            self.client.upload_file("/a", "/b")


if __name__ == "__main__":
    unittest.main()
