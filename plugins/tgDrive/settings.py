"""
Plugin settings resolution and the remote backup layout contract.

Settings configured in Stash (Settings -> Plugins) live in Stash's own config
and are read back through GraphQL `configuration.plugins` (see stash_client).
Precedence: CLI arguments > stdin payload > Stash plugin settings > defaults.

load_settings always returns a dict containing every key in
SETTINGS_DEFAULTS, with the type implied by its coercer (bool / float / int /
stripped str). Consumers can trust the values without re-coercing.
"""
from typing import Any, Callable, Dict, Optional

# Remote paths (fixed "latest snapshot" slots; each backup run replaces them).
REMOTE_METADATA_EXPORT = "/stash-metadata/export.zip"
REMOTE_DATABASE_BACKUP = "/stash-backup/database/stash-backup.zip"
REMOTE_CONFIG_BACKUP = "/stash-backup/config/config.yml"

# Top-level remote directories owned by this plugin. Restore skips them when
# rebuilding the archival media tree so archives and Telegram-compressed
# browse copies never leak into the Stash library.
SERVICE_ROOTS = ("stash-metadata", "stash-backup", "stash-browse")
REMOTE_BROWSE_ROOT = "/stash-browse"

SETTINGS_DEFAULTS: Dict[str, Any] = {
    "backup_scenes": True,
    "backup_metadata": True,
    "backup_database": True,
    "backup_config": False,
    "scene_upload_mode": "archive",
    "max_file_size_gb": 2.0,
    "batch_size": 50,
    "job_timeout_minutes": 120,
    "server_timeout_seconds": 120,
    "export_timeout_seconds": 1800,
    "td_binary_path": "",
    "td_data_dir": "",
    "td_version": "latest",
    "target_channel": "",
    "restore_dir": "",
}


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "1", "on"):
            return True
        if lowered in ("false", "no", "0", "off", ""):
            return False
    return default


def _coerce_float(value: Any, default: float) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: int) -> int:
    return int(_coerce_float(value, default))


def _coerce_str(value: Any, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _coerce_scene_upload_mode(value: Any, default: str) -> str:
    mode = _coerce_str(value, default).lower()
    return mode if mode in ("archive", "browse") else default


def _first_present(*sources: Optional[Dict[str, Any]], key: str) -> Any:
    for source in sources:
        if source and key in source and source[key] is not None:
            return source[key]
    return None


# Setting key -> coercer. Defaults live in SETTINGS_DEFAULTS; adding a setting
# is one entry here plus one default there.
FIELD_COERCERS: Dict[str, Callable[[Any, Any], Any]] = {
    "backup_scenes": _coerce_bool,
    "backup_metadata": _coerce_bool,
    "backup_database": _coerce_bool,
    "backup_config": _coerce_bool,
    "scene_upload_mode": _coerce_scene_upload_mode,
    "max_file_size_gb": _coerce_float,
    "batch_size": _coerce_int,
    "job_timeout_minutes": _coerce_int,
    "server_timeout_seconds": _coerce_int,
    "export_timeout_seconds": _coerce_int,
    "td_binary_path": _coerce_str,
    "td_data_dir": _coerce_str,
    "td_version": _coerce_str,
    "target_channel": _coerce_str,
    "restore_dir": _coerce_str,
}


def load_settings(
    stash=None,
    plugin_id: Optional[str] = None,
    payload_settings: Optional[Dict[str, Any]] = None,
    cli_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Merge settings sources into one typed dict.

    stash/plugin_id: used to read settings from Stash's config via GraphQL.
    payload_settings: settings embedded in the raw stdin payload (standalone
    invocations, tests).
    cli_overrides: explicit CLI arguments; keys absent from the command line
    must be None here.
    """
    stash_settings: Dict[str, Any] = {}
    if stash is not None and plugin_id:
        stash_settings = stash.get_plugin_settings(plugin_id) or {}

    return {
        key: coerce(
            _first_present(cli_overrides, payload_settings, stash_settings, key=key),
            SETTINGS_DEFAULTS[key],
        )
        for key, coerce in FIELD_COERCERS.items()
    }
