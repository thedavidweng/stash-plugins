import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from setup_check import build_setup_report, run_health_check
from td_client import TDClient


def make_td(data_dir):
    td = MagicMock()
    td.td_path = "/plugins/tgDrive/bin/td"
    td.data_dir = data_dir
    return td


class TestSetupCheck(unittest.TestCase):
    def test_reports_ready_authenticated_drive(self):
        with tempfile.TemporaryDirectory() as data_dir:
            td = make_td(data_dir)
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

            report = run_health_check(td, "configured binary")

        self.assertTrue(report["ready"])
        self.assertEqual(report["checks"]["binary"]["status"], "pass")
        self.assertEqual(report["checks"]["authentication"]["status"], "pass")
        self.assertEqual(report["checks"]["drive"]["status"], "pass")
        self.assertEqual(report["td"]["version"], "1.2.3")
        self.assertNotIn("Backup Account", str(report))

    def test_doctor_warning_does_not_block_readiness(self):
        with tempfile.TemporaryDirectory() as data_dir:
            td = make_td(data_dir)
            td.version.return_value = {"version": "1.2.3"}
            td.auth_status.return_value = {"authenticated": True}
            td.doctor.side_effect = RuntimeError("telegram unreachable")
            td.status.return_value = {"channel_id": "123"}

            report = run_health_check(td, "plugin directory")

        self.assertEqual(report["checks"]["telegram"]["status"], "warn")
        self.assertTrue(report["ready"])

    def test_explains_terminal_login_when_not_authenticated(self):
        with tempfile.TemporaryDirectory() as data_dir:
            td = make_td(data_dir)
            td.version.return_value = {"version": "1.2.3"}
            td.auth_status.return_value = {"authenticated": False}

            report = run_health_check(td, "plugin directory")

        self.assertFalse(report["ready"])
        self.assertEqual(report["checks"]["authentication"]["status"], "fail")
        self.assertIn("terminal", report["checks"]["authentication"]["hint"].lower())
        self.assertIn("SETUP.md", report["setup_guide"])
        td.doctor.assert_not_called()
        td.status.assert_not_called()

    def test_reports_missing_data_directory_actionably(self):
        with tempfile.TemporaryDirectory() as parent:
            td = make_td(os.path.join(parent, "missing"))
            td.version.return_value = {"version": "1.2.3"}
            td.auth_status.side_effect = RuntimeError("config missing")

            report = run_health_check(td, "configured binary")

        self.assertFalse(report["ready"])
        self.assertEqual(report["checks"]["data_directory"]["status"], "warn")
        self.assertIn("create", report["checks"]["data_directory"]["hint"].lower())


class TestSetupReport(unittest.TestCase):
    def test_commands_match_the_real_invocation_shape(self):
        """Setup instructions must match what TDClient actually executes."""
        with tempfile.TemporaryDirectory() as data_dir:
            td = TDClient(td_path="/opt/td", data_dir=data_dir)
            report = build_setup_report(td, "plugin directory")

        self.assertEqual(report["status"], "setup_required")
        self.assertFalse(report["ready"])
        self.assertEqual(report["td"]["path"], "/opt/td")
        self.assertEqual(report["data_directory"], data_dir)
        self.assertNotIn("error", report["td"])
        # commands are copy-pasteable and carry the session flags
        self.assertEqual(
            report["commands"]["auth_status"],
            f"/opt/td auth --config {data_dir}/config.toml "
            f"--session {data_dir}/session.json --db {data_dir}/local_cache.db status",
        )
        self.assertNotIn("--json", report["commands"]["auth_setup"])

    def test_resolution_error_is_reported_without_caller_mutation(self):
        with tempfile.TemporaryDirectory() as data_dir:
            td = TDClient(td_path="/opt/td", data_dir=data_dir)
            report = build_setup_report(td, "not resolved", error="td was not found")

        self.assertEqual(report["td"]["error"], "td was not found")


if __name__ == "__main__":
    unittest.main()
