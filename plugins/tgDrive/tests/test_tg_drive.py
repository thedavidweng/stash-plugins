import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger import Ledger
from tg_drive import (
    TD_REQUIRED_MODES,
    TaskContext,
    _channel_ledger_name,
    _ledger,
    get_plugin_id,
    get_td_data_dir,
    parse_args,
)


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

    @patch("tg_drive.get_plugin_dir")
    def test_unscoped_legacy_ledger_is_not_copied_to_new_channels(self, mock_plugin_dir):
        with tempfile.TemporaryDirectory() as plugin_dir, tempfile.TemporaryDirectory() as data_dir:
            mock_plugin_dir.return_value = plugin_dir
            Ledger(os.path.join(plugin_dir, "ledger.sqlite")).record_success("/scene.mp4", "scene:archive", 10)
            ctx = MagicMock(spec=TaskContext)
            ctx.data_dir = data_dir
            for channel in ("first", "second"):
                ctx.channel = channel
                self.assertIsNone(_ledger(ctx).get("/scene.mp4"))
            self.assertIsNotNone(Ledger(os.path.join(plugin_dir, "ledger.sqlite")).get("/scene.mp4"))

    def test_channel_ledger_name_is_file_safe(self):
        self.assertEqual(_channel_ledger_name("3984479327"), "ledger-3984479327.sqlite")
        self.assertEqual(_channel_ledger_name("NSFW"), "ledger-NSFW.sqlite")
        # unsafe characters are collapsed; the name can never escape the dir
        self.assertEqual(_channel_ledger_name("a/b\\c:d"), "ledger-a_b_c_d.sqlite")
        self.assertEqual(_channel_ledger_name(""), "ledger-channel.sqlite")

    def test_ledger_scopes_by_channel(self):
        """Explicit channels get per-channel ledgers; unset channels the default."""
        with tempfile.TemporaryDirectory() as data_dir:
            scoped = MagicMock(spec=TaskContext)
            scoped.data_dir = data_dir
            scoped.channel = "3984479327"
            ledger = _ledger(scoped)
            self.assertEqual(ledger.db_path, os.path.join(data_dir, "ledger-3984479327.sqlite"))

            unscoped = MagicMock(spec=TaskContext)
            unscoped.data_dir = data_dir
            unscoped.channel = None
            default_ledger = _ledger(unscoped)
            self.assertEqual(default_ledger.db_path, os.path.join(data_dir, "ledger.sqlite"))

    def test_run_backup_requires_explicit_channel(self):
        """A backup without Target Telegram Channel is refused, never defaulted."""
        from tg_drive import run_backup

        ctx = MagicMock(spec=TaskContext)
        ctx.channel = None
        ctx.settings = {}
        ctx.stash = MagicMock()
        ctx.td = MagicMock()
        ctx.dry_run = False
        ctx.data_dir = "/tmp"

        output = run_backup(ctx)
        self.assertEqual(output["status"], "error")
        self.assertIn("Target Telegram Channel", output["message"])
        ctx.td.cp.assert_not_called()
        ctx.td.upload_file.assert_not_called()
        ctx.stash.find_scenes.assert_not_called()


if __name__ == "__main__":
    unittest.main()
