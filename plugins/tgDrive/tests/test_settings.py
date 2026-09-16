import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from settings import load_settings


def make_stash(stash_settings):
    stash = MagicMock()
    stash.get_plugin_settings.return_value = stash_settings
    return stash


class TestLoadSettings(unittest.TestCase):
    def test_defaults(self):
        settings = load_settings(stash=None, plugin_id=None)
        self.assertTrue(settings["backup_scenes"])
        self.assertTrue(settings["backup_metadata"])
        self.assertTrue(settings["backup_database"])
        self.assertFalse(settings["backup_config"])
        self.assertEqual(settings["max_file_size_gb"], 2.0)
        self.assertEqual(settings["batch_size"], 50)
        self.assertEqual(settings["td_data_dir"], "")
        self.assertEqual(settings["td_version"], "latest")

    def test_stash_settings_used(self):
        stash = make_stash({
            "backup_scenes": False,
            "backup_config": True,
            "max_file_size_gb": 4,
            "target_channel": "@mydrive",
            "td_data_dir": "/td-data",
            "td_version": "v1.2.3",
        })
        settings = load_settings(stash=stash, plugin_id="tgDrive")
        stash.get_plugin_settings.assert_called_once_with("tgDrive")
        self.assertFalse(settings["backup_scenes"])
        self.assertTrue(settings["backup_config"])
        self.assertEqual(settings["max_file_size_gb"], 4.0)
        self.assertEqual(settings["target_channel"], "@mydrive")
        self.assertEqual(settings["td_data_dir"], "/td-data")
        self.assertEqual(settings["td_version"], "v1.2.3")
        # untouched keys keep defaults
        self.assertTrue(settings["backup_metadata"])

    def test_payload_overrides_stash_settings(self):
        stash = make_stash({"backup_scenes": False})
        settings = load_settings(
            stash=stash,
            plugin_id="tgDrive",
            payload_settings={"backup_scenes": True},
        )
        self.assertTrue(settings["backup_scenes"])

    def test_cli_overrides_everything(self):
        stash = make_stash({"backup_scenes": False, "max_file_size_gb": 4})
        settings = load_settings(
            stash=stash,
            plugin_id="tgDrive",
            payload_settings={"backup_scenes": True},
            cli_overrides={"backup_scenes": False},
        )
        self.assertFalse(settings["backup_scenes"])

    def test_none_cli_values_are_ignored(self):
        stash = make_stash({"max_file_size_gb": 4})
        settings = load_settings(
            stash=stash,
            plugin_id="tgDrive",
            cli_overrides={"max_file_size_gb": None, "restore_dir": None},
        )
        self.assertEqual(settings["max_file_size_gb"], 4.0)
        self.assertEqual(settings["restore_dir"], "")

    def test_string_bools_coerced(self):
        # Stash stores raw yml-typed values; be liberal in what we accept
        settings = load_settings(payload_settings={
            "backup_scenes": "false",
            "backup_config": "true",
            "batch_size": "25",
            "job_timeout_minutes": "90",
        })
        self.assertFalse(settings["backup_scenes"])
        self.assertTrue(settings["backup_config"])
        self.assertEqual(settings["batch_size"], 25)
        self.assertEqual(settings["job_timeout_minutes"], 90)

    def test_bad_values_fall_back(self):
        settings = load_settings(payload_settings={
            "max_file_size_gb": "not-a-number",
            "batch_size": None,
        })
        self.assertEqual(settings["max_file_size_gb"], 2.0)
        self.assertEqual(settings["batch_size"], 50)


if __name__ == "__main__":
    unittest.main()
