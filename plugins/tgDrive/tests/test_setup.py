import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from setup_check import run_health_check


class TestSetupCheck(unittest.TestCase):
    def test_reports_ready_authenticated_drive(self):
        with tempfile.TemporaryDirectory() as data_dir:
            td = MagicMock()
            td.version.return_value = {"version": "1.2.3"}
            td.auth_status.return_value = {
                "authenticated": True,
                "display_name": "Backup Account",
            }
            td.doctor.return_value = {
                "checks": {"auth": "pass", "db": "pass"},
                "max_upload_bytes": 2147483648,
            }
            td.status.return_value = {
                "channel_id": "123",
                "authenticated": True,
                "files": {"active": 2},
            }

            report = run_health_check(
                td=td,
                td_path="/plugins/tgDrive/bin/td",
                td_source="configured binary",
                data_dir=data_dir,
            )

        self.assertTrue(report["ready"])
        self.assertEqual(report["checks"]["binary"]["status"], "pass")
        self.assertEqual(report["checks"]["authentication"]["status"], "pass")
        self.assertEqual(report["checks"]["drive"]["status"], "pass")
        self.assertEqual(report["td"]["version"], "1.2.3")
        self.assertNotIn("Backup Account", str(report))

    def test_explains_terminal_login_when_not_authenticated(self):
        with tempfile.TemporaryDirectory() as data_dir:
            td = MagicMock()
            td.version.return_value = {"version": "1.2.3"}
            td.auth_status.return_value = {"authenticated": False}

            report = run_health_check(
                td=td,
                td_path="/plugins/tgDrive/bin/td",
                td_source="plugin directory",
                data_dir=data_dir,
            )

        self.assertFalse(report["ready"])
        self.assertEqual(report["checks"]["authentication"]["status"], "fail")
        self.assertIn("terminal", report["checks"]["authentication"]["hint"].lower())
        self.assertIn("SETUP.md", report["setup_guide"])
        td.doctor.assert_not_called()
        td.status.assert_not_called()

    def test_reports_missing_data_directory_actionably(self):
        with tempfile.TemporaryDirectory() as parent:
            data_dir = os.path.join(parent, "missing")
            td = MagicMock()
            td.version.return_value = {"version": "1.2.3"}
            td.auth_status.side_effect = RuntimeError("config missing")

            report = run_health_check(
                td=td,
                td_path="/plugins/tgDrive/bin/td",
                td_source="configured binary",
                data_dir=data_dir,
            )

        self.assertFalse(report["ready"])
        self.assertEqual(report["checks"]["data_directory"]["status"], "warn")
        self.assertIn("create", report["checks"]["data_directory"]["hint"].lower())


if __name__ == "__main__":
    unittest.main()
