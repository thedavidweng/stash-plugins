"""
Main entry point for the tgDrive Stash plugin.
Implements the raw interface protocol (stdin payload -> stdout response).
Can also be executed standalone from the command line.

All Stash interaction goes through the GraphQL API (see stash_client.py);
this plugin never reads or writes Stash's SQLite database directly. Plugin
settings configured in Stash's UI are read back through GraphQL
`configuration.plugins` (raw plugins do not receive them on stdin).
"""
import argparse
import json
import os
import sys
from typing import Any, Dict, Optional

from ledger import Ledger
import log
from restore_engine import RestoreEngine
from settings import load_settings
from setup_check import SETUP_GUIDE_URL, build_setup_report, run_health_check
from stash_client import StashClient
from sync_engine import SyncEngine
from td_client import TDClient
from td_resolver import resolve_td_path
from td_updater import install_td_core


def get_plugin_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def get_plugin_id() -> str:
    """Stash identifies plugins by the yml file name, which is the directory name."""
    return os.path.basename(get_plugin_dir())


def get_td_data_dir(settings: Dict[str, Any]) -> str:
    configured = str(settings.get("td_data_dir") or "").strip()
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Stash <-> Telegram Backup Bridge Plugin")
    parser.add_argument(
        "--mode",
        choices=[
            "setup",
            "health",
            "install_core",
            "backup",
            "dry_run",
            "verify",
            "restore",
        ],
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
    cli_args = parser.parse_args()

    # Read stdin payload from Stash
    payload = parse_args_from_stdin()

    server_conn = payload.get("server_connection", {})
    task_args = payload.get("args", {})
    payload_settings = payload.get("settings", {}) or {}

    # Determine mode (task defaultArgs arrive via stdin)
    mode = task_args.get("mode") or cli_args.mode

    # Configure Stash Client (GraphQL only)
    scheme = server_conn.get("Scheme", "http")
    host = server_conn.get("Host", "localhost")
    port = server_conn.get("Port", 9999)
    base_url = cli_args.server_url or f"{scheme}://{host}:{port}"
    api_key = cli_args.api_key or server_conn.get("ApiKey")
    session_cookie = server_conn.get("SessionCookie")

    stash = StashClient(
        base_url=base_url,
        api_key=api_key,
        session_cookie=session_cookie,
    )

    # Resolve settings: CLI > stdin payload > Stash plugin settings (GraphQL) > defaults
    cli_overrides: Dict[str, Optional[Any]] = {
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
    }
    settings = load_settings(
        stash=stash,
        plugin_id=get_plugin_id(),
        payload_settings=payload_settings,
        cli_overrides=cli_overrides,
    )

    # Apply timeout settings to the client (longer budget for synchronous
    # export/backup mutations)
    stash.timeout = int(float(settings.get("server_timeout_seconds", 120)))
    stash.long_timeout = int(float(settings.get("export_timeout_seconds", 1800)))

    output: Dict[str, Any] = {}
    try:
        data_dir = get_td_data_dir(settings)
        configured_data_dir = bool(str(settings.get("td_data_dir") or "").strip())
        if not configured_data_dir:
            os.makedirs(data_dir, exist_ok=True)

        if mode == "setup":
            try:
                resolution = resolve_td_path(
                    settings.get("td_binary_path"),
                    get_plugin_dir(),
                )
                setup_report = build_setup_report(
                    resolution.path,
                    data_dir,
                    resolution.source,
                )
            except ValueError as exc:
                setup_report = build_setup_report(
                    settings.get("td_binary_path") or "<td path not resolved>",
                    data_dir,
                )
                setup_report["td"]["error"] = str(exc)
            output = {"status": "ok", "setup": setup_report}
            print(json.dumps({"output": output}))
            return

        if mode == "install_core":
            configured_path = str(settings.get("td_binary_path") or "").strip()
            if configured_path:
                output = {
                    "status": "error",
                    "message": (
                        "TD Core Path is explicitly configured. Clear it before "
                        "installing the plugin-managed td binary, or install the "
                        "release archive manually at the configured path."
                    ),
                    "setup_guide": SETUP_GUIDE_URL,
                }
            else:
                try:
                    output = {
                        "status": "ok",
                        "install": install_td_core(
                            get_plugin_dir(),
                            version=settings.get("td_version") or "latest",
                        ),
                        "message": (
                            "td was installed into the plugin directory. Run "
                            "TD Drive Health Check next."
                        ),
                        "setup_guide": SETUP_GUIDE_URL,
                    }
                except Exception as exc:
                    output = {
                        "status": "error",
                        "message": str(exc),
                        "setup_guide": SETUP_GUIDE_URL,
                    }
            print(json.dumps({"output": output}))
            return

        try:
            resolution = resolve_td_path(
                settings.get("td_binary_path"),
                get_plugin_dir(),
            )
        except ValueError as exc:
            if mode in ("health", "backup", "restore"):
                output = {
                    "status": "error",
                    "message": str(exc),
                    "setup_guide": SETUP_GUIDE_URL,
                }
                print(json.dumps({"output": output}))
                return
            resolution = None

        td = TDClient(
            td_path=resolution.path if resolution else (settings.get("td_binary_path") or "td"),
            data_dir=data_dir,
            channel=settings.get("target_channel") or None,
        )

        if mode == "health":
            output = {
                "status": "ok",
                "health": run_health_check(
                    td=td,
                    td_path=td.td_path,
                    td_source=resolution.source if resolution else "not resolved",
                    data_dir=data_dir,
                ),
            }
            print(json.dumps({"output": output}))
            return

        # Configure Ledger only for modes that use the local sync index.
        ledger_path = os.path.join(get_plugin_dir(), "ledger.sqlite")
        ledger = Ledger(ledger_path)

        if mode in ("backup", "dry_run"):
            if mode == "backup":
                health = run_health_check(
                    td=td,
                    td_path=td.td_path,
                    td_source=resolution.source if resolution else "not resolved",
                    data_dir=data_dir,
                )
                if not health["ready"]:
                    output = {
                        "status": "error",
                        "message": (
                            "TD Drive is not ready. Complete the setup shown by "
                            "the TD Drive Setup / Health Check task."
                        ),
                        "setup_guide": SETUP_GUIDE_URL,
                        "health": health,
                    }
                    print(json.dumps({"output": output}))
                    return
            engine = SyncEngine(
                stash=stash,
                td=td,
                ledger=ledger,
                settings=settings,
                dry_run=(mode == "dry_run"),
            )
            report = engine.run_backup()
            output = {"status": "ok", "report": report}

        elif mode == "verify":
            log.LogInfo("Running ledger and hash verification...")
            summary = ledger.get_summary()
            output = {"status": "ok", "ledger_summary": summary}
            log.LogInfo(f"Verification complete: {summary}")

        elif mode == "restore":
            health = run_health_check(
                td=td,
                td_path=td.td_path,
                td_source=resolution.source if resolution else "not resolved",
                data_dir=data_dir,
            )
            if not health["ready"]:
                output = {
                    "status": "error",
                    "message": (
                        "TD Drive is not ready. Complete the setup shown by "
                        "the TD Drive Setup / Health Check task."
                    ),
                    "setup_guide": SETUP_GUIDE_URL,
                    "health": health,
                }
                print(json.dumps({"output": output}))
                return
            restore_dir = settings.get("restore_dir")
            if not restore_dir:
                output = {
                    "status": "error",
                    "message": "restore_dir is not configured. Set it in Stash Settings -> Plugins -> TG Drive Backup.",
                }
            else:
                engine = RestoreEngine(
                    stash=stash,
                    td=td,
                    restore_dir=restore_dir,
                    job_timeout_seconds=int(float(settings.get("job_timeout_minutes", 120)) * 60),
                )
                report = engine.run_restore()
                output = {"status": "ok", "report": report}

        else:
            output = {"status": "error", "message": f"Unknown mode: {mode}"}

        print(json.dumps({"output": output}))

    except Exception as e:
        log.LogError(f"Plugin execution failed: {e}")
        import traceback
        log.LogDebug(traceback.format_exc())
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
