"""
Main entry point for the tgDrive Stash plugin.
Implements the raw interface protocol (stdin payload -> stdout response).
Can also be executed standalone from the command line.
"""
import argparse
import json
import os
import sys
from typing import Any, Dict

from ledger import Ledger
import log
from restore_engine import RestoreEngine
from stash_client import StashClient
from sync_engine import SyncEngine
from td_client import TDClient


def get_plugin_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


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
    parser.add_argument("--mode", choices=["backup", "dry_run", "verify", "restore"], default="backup")
    parser.add_argument("--td-path", default=None, help="Path to td binary")
    parser.add_argument("--channel", default=None, help="Target Telegram channel ID or username")
    parser.add_argument("--max-size-gb", type=float, default=2.0, help="Maximum file size in GB")
    parser.add_argument("--batch-size", type=int, default=50, help="Maximum scenes to process per run")
    parser.add_argument("--restore-dir", default=None, help="Directory to restore media into")
    parser.add_argument("--sqlite-path", default=None, help="Path to stash-go.sqlite fallback")
    cli_args = parser.parse_args()

    # Read stdin payload from Stash
    payload = parse_args_from_stdin()

    server_conn = payload.get("server_connection", {})
    task_args = payload.get("args", {})
    plugin_settings = payload.get("settings", {})

    # Determine mode
    mode = task_args.get("mode") or cli_args.mode

    # Configure Stash Client
    scheme = server_conn.get("Scheme", "http")
    host = server_conn.get("Host", "localhost")
    port = server_conn.get("Port", 9999)
    base_url = f"{scheme}://{host}:{port}"
    api_key = server_conn.get("ApiKey") or plugin_settings.get("api_key")
    session_cookie = server_conn.get("SessionCookie")

    sqlite_path = cli_args.sqlite_path or plugin_settings.get("sqlite_path")
    if not sqlite_path:
        default_sqlite = "/root/.stash/stash-go.sqlite"
        if os.path.exists(default_sqlite):
            sqlite_path = default_sqlite

    stash = StashClient(
        base_url=base_url,
        api_key=api_key,
        session_cookie=session_cookie,
        sqlite_path=sqlite_path,
    )

    # Configure TD Client
    td_path = plugin_settings.get("td_binary_path") or cli_args.td_path
    channel = plugin_settings.get("target_channel") or cli_args.channel
    td = TDClient(td_path=td_path, channel=channel)

    # Configure Ledger
    ledger_path = os.path.join(get_plugin_dir(), "ledger.sqlite")
    ledger = Ledger(ledger_path)

    # Configure Max File Size
    max_gb = float(plugin_settings.get("max_file_size_gb") or cli_args.max_size_gb)
    max_bytes = int(max_gb * 1024 * 1024 * 1024)

    # Configure Batch Size
    batch_size = int(plugin_settings.get("batch_size") or cli_args.batch_size)

    output = {}
    try:
        if mode in ("backup", "dry_run"):
            engine = SyncEngine(
                stash=stash,
                td=td,
                ledger=ledger,
                max_file_size_bytes=max_bytes,
                dry_run=(mode == "dry_run"),
            )
            report = engine.run_backup(batch_limit=batch_size)
            output = {"status": "ok", "report": report}

        elif mode == "verify":
            log.LogInfo("Running ledger and hash verification...")
            summary = ledger.get_summary()
            output = {"status": "ok", "ledger_summary": summary}
            log.LogInfo(f"Verification complete: {summary}")

        elif mode == "restore":
            restore_dir = cli_args.restore_dir or plugin_settings.get("restore_dir") or "/data"
            engine = RestoreEngine(stash=stash, td=td, target_dir=restore_dir)
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
