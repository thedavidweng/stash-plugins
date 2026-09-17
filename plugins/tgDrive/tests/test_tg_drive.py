import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger import Ledger
from tg_drive import TD_REQUIRED_MODES, TaskContext, _ledger, get_plugin_id, get_td_data_dir, parse_args


class TestHelpers(unittest.TestCase):
    def test_data_dir_defaults_to_plugin_dir(self):
        settings = {"td_data_dir": ""}
        self.assertEqual(
            get_td_data_dir(settings),
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "td-data"),
        )

    def test_data_dir_expands_configured_path(self):
        settings = {"td_data_dir": "~/.stash-tg-drive"}
        expected = os.path.abspath(os.path.expanduser("~/.stash-tg-drive"))
        self.assertEqual(get_td_data_dir(settings), expected)

    def test_plugin_id_is_directory_name(self):
        self.assertEqual(get_plugin_id(), "tgDrive")

    def test_cli_modes_cover_every_handler(self):
        """argparse choices and the dispatch table must stay in sync."""
        from tg_drive import HANDLERS
        for mode in HANDLERS:
            self.assertEqual(parse_args(["--mode", mode]).mode, mode)
        with self.assertRaises(SystemExit):
            parse_args(["--mode", "not-a-mode"])

    def test_td_required_modes_are_dispatched(self):
        # TD-required modes must exist as handlers, or they could never run.
        from tg_drive import HANDLERS
        self.assertTrue(set(TD_REQUIRED_MODES) <= set(HANDLERS))

    @patch("tg_drive.get_plugin_dir")
    def test_ledger_migrates_to_persistent_data_directory(self, mock_plugin_dir):
        with tempfile.TemporaryDirectory() as plugin_dir, tempfile.TemporaryDirectory() as data_dir:
            mock_plugin_dir.return_value = plugin_dir
            legacy = os.path.join(plugin_dir, "ledger.sqlite")
            Ledger(legacy).record_success("/scene.mp4", "scene:archive", 10)
            ctx = MagicMock(spec=TaskContext)
            ctx.data_dir = data_dir

            migrated = _ledger(ctx)

            self.assertEqual(migrated.get("/scene.mp4")["status"], "published")
            self.assertTrue(os.path.exists(os.path.join(data_dir, "ledger.sqlite")))


if __name__ == "__main__":
    unittest.main()
