"""
Main entry point for the tgDrive Stash plugin.
Implements the raw interface protocol (stdin payload -> stdout response).
Can also be executed standalone from the command line.

All Stash interaction goes through the GraphQL API (see stash_client.py);
this plugin never reads or writes Stash's SQLite database directly. Plugin
settings configured in Stash's UI are read back through GraphQL
`configuration.plugins` (raw plugins do not receive them on stdin).

Each Stash task (see tgDrive.yml) selects a mode; modes are dispatched
through HANDLERS to small handler functions that all return the output
payload. main() only wires up dependencies (Stash client, settings, td
resolution) and prints the result once.
"""
import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from ledger import Ledger
import log
from restore_engine import RestoreEngine
from settings import load_settings
from setup_check import SETUP_GUIDE_URL, build_setup_report, run_health_check
from stash_client import StashClient
from sync_engine import SyncEngine
from td_client import TDClient
from td_resolver import TDResolution, resolve_td_path
from td_updater import install_td_core

# Modes that need a working td binary; the others never invoke td.
TD_REQUIRED_MODES = ("health", "backup", "restore")

NOT_READY_MESSAGE = (
    "TD Drive is not ready. Complete the setup shown by "
    "the TD Drive Setup / Health Check task."
)


def get_plugin_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def get_plugin_id() -> str:
    """Stash identifies plugins by the yml file name, which is the directory name."""
    return os.path.basename(get_plugin_dir())


def get_td_data_dir(settings: Dict[str, Any]) -> str:
    configured = settings["td_data_dir"]
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    return os.path.join(get_plugin_dir(), "td-data")


def parse_args_from_stdin() -> Dict[str, Any]:
    """Read the Stash plugin raw interface payload from stdin if available."""
    if not sys.stdin.isatty():
        try:
            content = sys.stdin.read().strip()
            if content:
                return json.loads(content)
        except Exception as e:
            log.LogWarning(f"Failed to parse stdin payload: {e}")
    return {}


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stash <-> Telegram Backup Bridge Plugin")
    parser.add_argument(
        "--mode",
        choices=list(HANDLERS),
        default="backup",
    )
    parser.add_argument("--server-url", default=None, help="Stash server URL (default http://localhost:9999)")
    parser.add_argument("--api-key", default=None, help="Stash API key (standalone mode)")
    parser.add_argument("--td-path", default=None, help="Path to td binary")
    parser.add_argument("--td-data-dir", default=None, help="Persistent td config/session/cache directory")
    parser.add_argument("--td-version", default=None, help="td release tag or latest")
    parser.add_argument("--channel", default=None, help="Target Telegram channel ID or username")
    parser.add_argument("--max-size-gb", type=float, default=None, help="Maximum file size in GB")
    parser.add_argument("--batch-size", type=int, default=None, help="Maximum scenes to process per run")
    parser.add_argument("--restore-dir", default=None, help="Directory to restore media into")
    parser.add_argument("--backup-scenes", dest="backup_scenes", type=str, default=None, help="true/false")
    parser.add_argument("--backup-metadata", dest="backup_metadata", type=str, default=None, help="true/false")
    parser.add_argument("--backup-database", dest="backup_database", type=str, default=None, help="true/false")
    parser.add_argument("--backup-config", dest="backup_config", type=str, default=None, help="true/false")
    return parser.parse_args(argv)


@dataclass
class TaskContext:
    """Per-run dependencies shared by the mode handlers."""

    stash: StashClient
    settings: Dict[str, Any]
    data_dir: str
    resolution: Optional[TDResolution]
    resolution_error: Optional[ValueError]
    td_source: str
    td: TDClient
    dry_run: bool


def _build_context(mode: str, stash: StashClient, settings: Dict[str, Any]) -> TaskContext:
    data_dir = get_td_data_dir(settings)
    if not settings["td_data_dir"]:
        os.makedirs(data_dir, exist_ok=True)

    try:
        resolution = resolve_td_path(settings["td_binary_path"], get_plugin_dir())
        resolution_error = None
    except ValueError as exc:
        resolution = None
        resolution_error = exc

    td = TDClient(
        td_path=resolution.path if resolution else (settings["td_binary_path"] or "td"),
        data_dir=data_dir,
        channel=settings["target_channel"] or None,
    )
    return TaskContext(
        stash=stash,
        settings=settings,
        data_dir=data_dir,
        resolution=resolution,
        resolution_error=resolution_error,
        td_source=resolution.source if resolution else "not resolved",
        td=td,
        dry_run=(mode == "dry_run"),
    )


def _ledger(ctx: TaskContext) -> Ledger:
    """Open the persistent sync index, migrating the legacy plugin-local file."""
    os.makedirs(ctx.data_dir, exist_ok=True)
    target = os.path.join(ctx.data_dir, "ledger.sqlite")
    legacy = os.path.join(get_plugin_dir(), "ledger.sqlite")
    if not os.path.exists(target) and os.path.exists(legacy) and legacy != target:
        shutil.copy2(legacy, target)
        log.LogInfo(f"Migrated the sync ledger to persistent TD data directory: {target}")
    return Ledger(target)


def _setup_error(message: str) -> Dict[str, Any]:
    # Raw-interface stdout JSON is only visible in the task panel; the log
    # is where operators look first, so mirror the reason there.
    log.LogWarning(f"TG Drive setup error: {message}")
    return {"status": "error", "message": message, "setup_guide": SETUP_GUIDE_URL}


def _require_ready_td(ctx: TaskContext) -> Optional[Dict[str, Any]]:
    """Return the not-ready error output, or None when the drive is usable."""
    health = run_health_check(ctx.td, ctx.td_source)
    if health["ready"]:
        return None
    problems = {
        name: check["status"]
        for name, check in health["checks"].items()
        if check["status"] != "pass"
    }
    log.LogWarning(f"TD Drive is not ready; failing checks: {problems}")
    return {
        "status": "error",
        "message": NOT_READY_MESSAGE,
        "setup_guide": SETUP_GUIDE_URL,
        "health": health,
    }


# ------------------------------------------------------------ mode handlers


def run_setup(ctx: TaskContext) -> Dict[str, Any]:
    setup = build_setup_report(
        ctx.td,
        ctx.td_source,
        error=str(ctx.resolution_error) if ctx.resolution_error else None,
    )
    return {"status": "ok", "setup": setup}


def run_install_core(ctx: TaskContext) -> Dict[str, Any]:
    if ctx.settings["td_binary_path"]:
        return _setup_error(
            "TD Core Path is explicitly configured. Clear it before "
            "installing the plugin-managed td binary, or install the "
            "release archive manually at the configured path."
        )
    try:
        install = install_td_core(get_plugin_dir(), version=ctx.settings["td_version"])
    except Exception as exc:
        return _setup_error(str(exc))
    return {
        "status": "ok",
        "install": install,
        "message": "td was installed into the plugin directory. Run TD Drive Health Check next.",
        "setup_guide": SETUP_GUIDE_URL,
    }


def run_health(ctx: TaskContext) -> Dict[str, Any]:
    return {"status": "ok", "health": run_health_check(ctx.td, ctx.td_source)}


def run_backup(ctx: TaskContext) -> Dict[str, Any]:
    if not ctx.dry_run:
        gate = _require_ready_td(ctx)
        if gate is not None:
            return gate
    engine = SyncEngine(
        stash=ctx.stash,
        td=ctx.td,
        ledger=_ledger(ctx),
        settings=ctx.settings,
        dry_run=ctx.dry_run,
    )
    return {"status": "ok", "report": engine.run_backup()}


def run_verify(ctx: TaskContext) -> Dict[str, Any]:
    log.LogInfo("Running ledger and hash verification...")
    summary = _ledger(ctx).get_summary()
    log.LogInfo(f"Verification complete: {summary}")
    return {"status": "ok", "ledger_summary": summary}


def run_restore(ctx: TaskContext) -> Dict[str, Any]:
    gate = _require_ready_td(ctx)
    if gate is not None:
        return gate
    restore_dir = ctx.settings["restore_dir"]
    if not restore_dir:
        return {
            "status": "error",
            "message": "restore_dir is not configured. Set it in Stash Settings -> Plugins -> TG Drive Backup.",
        }
    engine = RestoreEngine(
        stash=ctx.stash,
        td=ctx.td,
        restore_dir=restore_dir,
        job_timeout_seconds=ctx.settings["job_timeout_minutes"] * 60,
    )
    return {"status": "ok", "report": engine.run_restore()}


HANDLERS: Dict[str, Callable[[TaskContext], Dict[str, Any]]] = {
    "setup": run_setup,
    "install_core": run_install_core,
    "health": run_health,
    "backup": run_backup,
    "dry_run": run_backup,
    "verify": run_verify,
    "restore": run_restore,
}


# --------------------------------------------------------------------- main


def main() -> None:
    cli_args = parse_args()
    payload = parse_args_from_stdin()

    server_conn = payload.get("server_connection", {}) or {}
    task_args = payload.get("args", {}) or {}
    payload_settings = payload.get("settings", {}) or {}

    # Determine mode (task defaultArgs arrive via stdin)
    mode = task_args.get("mode") or cli_args.mode

    # Configure Stash Client (GraphQL only)
    scheme = server_conn.get("Scheme", "http")
    host = server_conn.get("Host", "localhost")
    port = server_conn.get("Port", 9999)
    stash = StashClient(
        base_url=cli_args.server_url or f"{scheme}://{host}:{port}",
        api_key=cli_args.api_key or server_conn.get("ApiKey"),
        session_cookie=server_conn.get("SessionCookie"),
    )

    # Resolve settings: CLI > stdin payload > Stash plugin settings (GraphQL) > defaults
    settings = load_settings(
        stash=stash,
        plugin_id=get_plugin_id(),
        payload_settings=payload_settings,
        cli_overrides={
            "backup_scenes": cli_args.backup_scenes,
            "backup_metadata": cli_args.backup_metadata,
            "backup_database": cli_args.backup_database,
            "backup_config": cli_args.backup_config,
            "max_file_size_gb": cli_args.max_size_gb,
            "batch_size": cli_args.batch_size,
            "td_binary_path": cli_args.td_path,
            "td_data_dir": cli_args.td_data_dir,
            "td_version": cli_args.td_version,
            "target_channel": cli_args.channel,
            "restore_dir": cli_args.restore_dir,
        },
    )

    # Apply timeout settings to the client (longer budget for synchronous
    # export/backup mutations)
    stash.timeout = settings["server_timeout_seconds"]
    stash.long_timeout = settings["export_timeout_seconds"]

    try:
        ctx = _build_context(mode, stash, settings)
        if ctx.resolution is None and mode in TD_REQUIRED_MODES:
            output = _setup_error(str(ctx.resolution_error))
        else:
            handler = HANDLERS.get(mode)
            if handler:
                output = handler(ctx)
            else:
                message = f"Unknown mode: {mode}"
                log.LogError(message)
                output = {"status": "error", "message": message}
    except Exception as e:
        log.LogError(f"Plugin execution failed: {e}")
        import traceback
        log.LogDebug(traceback.format_exc())
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    print(json.dumps({"output": output}))


if __name__ == "__main__":
    main()
