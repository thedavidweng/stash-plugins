"""
Plugin settings resolution and the remote backup layout contract.

Settings configured in Stash (Settings -> Plugins) live in Stash's own config
and are read back through GraphQL `configuration.plugins` (see stash_client).
Precedence: CLI arguments > stdin payload > Stash plugin settings > defaults.
"""
from typing import Any, Dict, Optional

# Remote paths (fixed "latest snapshot" slots; each backup run replaces them).
REMOTE_METADATA_EXPORT = "/stash-metadata/export.zip"
REMOTE_DATABASE_BACKUP = "/stash-backup/database/stash-backup.zip"
REMOTE_CONFIG_BACKUP = "/stash-backup/config/config.yml"

# Top-level remote directories owned by this plugin. Restore skips them when
# rebuilding the media tree so export/backup archives never leak into the
# Stash library (Stash would index .zip files as galleries).
SERVICE_ROOTS = ("stash-metadata", "stash-backup")

SETTINGS_DEFAULTS: Dict[str, Any] = {
    "backup_scenes": True,
    "backup_metadata": True,
    "backup_database": True,
    "backup_config": False,
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


def _coerce_number(value: Any, default: float) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_str(value: Any, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _first_present(*sources: Optional[Dict[str, Any]], key: str) -> Any:
    for source in sources:
        if source and key in source and source[key] is not None:
            return source[key]
    return None


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

    merged: Dict[str, Any] = {}

    merged["backup_scenes"] = _coerce_bool(
        _first_present(cli_overrides, payload_settings, stash_settings, key="backup_scenes"),
        SETTINGS_DEFAULTS["backup_scenes"],
    )
    merged["backup_metadata"] = _coerce_bool(
        _first_present(cli_overrides, payload_settings, stash_settings, key="backup_metadata"),
        SETTINGS_DEFAULTS["backup_metadata"],
    )
    merged["backup_database"] = _coerce_bool(
        _first_present(cli_overrides, payload_settings, stash_settings, key="backup_database"),
        SETTINGS_DEFAULTS["backup_database"],
    )
    merged["backup_config"] = _coerce_bool(
        _first_present(cli_overrides, payload_settings, stash_settings, key="backup_config"),
        SETTINGS_DEFAULTS["backup_config"],
    )
    merged["max_file_size_gb"] = _coerce_number(
        _first_present(cli_overrides, payload_settings, stash_settings, key="max_file_size_gb"),
        SETTINGS_DEFAULTS["max_file_size_gb"],
    )
    merged["batch_size"] = _coerce_number(
        _first_present(cli_overrides, payload_settings, stash_settings, key="batch_size"),
        SETTINGS_DEFAULTS["batch_size"],
    )
    merged["job_timeout_minutes"] = _coerce_number(
        _first_present(cli_overrides, payload_settings, stash_settings, key="job_timeout_minutes"),
        SETTINGS_DEFAULTS["job_timeout_minutes"],
    )
    merged["server_timeout_seconds"] = _coerce_number(
        _first_present(cli_overrides, payload_settings, stash_settings, key="server_timeout_seconds"),
        SETTINGS_DEFAULTS["server_timeout_seconds"],
    )
    merged["export_timeout_seconds"] = _coerce_number(
        _first_present(cli_overrides, payload_settings, stash_settings, key="export_timeout_seconds"),
        SETTINGS_DEFAULTS["export_timeout_seconds"],
    )
    merged["td_binary_path"] = _coerce_str(
        _first_present(cli_overrides, payload_settings, stash_settings, key="td_binary_path"),
        "",
    )
    merged["td_data_dir"] = _coerce_str(
        _first_present(cli_overrides, payload_settings, stash_settings, key="td_data_dir"),
        "",
    )
    merged["td_version"] = _coerce_str(
        _first_present(cli_overrides, payload_settings, stash_settings, key="td_version"),
        SETTINGS_DEFAULTS["td_version"],
    )
    merged["target_channel"] = _coerce_str(
        _first_present(cli_overrides, payload_settings, stash_settings, key="target_channel"),
        "",
    )
    merged["restore_dir"] = _coerce_str(
        _first_present(cli_overrides, payload_settings, stash_settings, key="restore_dir"),
        "",
    )
    return merged
