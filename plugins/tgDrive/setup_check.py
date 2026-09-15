"""User-facing setup and health reporting for the Stash task UI."""
import os
import shlex
from typing import Any, Dict


SETUP_GUIDE_URL = (
    "https://github.com/thedavidweng/stash-plugins/blob/main/"
    "plugins/tgDrive/SETUP.md"
)


def _check(status: str, message: str, hint: str = "") -> Dict[str, str]:
    result = {"status": status, "message": message}
    if hint:
        result["hint"] = hint
    return result


def _td_command(td_path: str, data_dir: str, subcommand: str) -> str:
    config = os.path.join(data_dir, "config.toml")
    session = os.path.join(data_dir, "session.json")
    database = os.path.join(data_dir, "local_cache.db")
    return " ".join(
        [
            shlex.quote(td_path),
            "--config",
            shlex.quote(config),
            "--session",
            shlex.quote(session),
            "--db",
            shlex.quote(database),
            subcommand,
        ]
    )


def build_setup_report(
    td_path: str,
    data_dir: str,
    td_source: str = "not resolved",
) -> Dict[str, Any]:
    """Return safe, copy-friendly setup information for the Stash UI."""
    data_dir = os.path.abspath(os.path.expanduser(data_dir))
    report: Dict[str, Any] = {
        "status": "setup_required",
        "ready": False,
        "setup_guide": SETUP_GUIDE_URL,
        "td": {"path": td_path, "source": td_source},
        "data_directory": data_dir,
        "message": (
            "Complete the one-time td setup in the Stash container terminal. "
            "Do not enter Telegram API hashes, login codes, or 2FA passwords "
            "in Stash plugin settings."
        ),
        "commands": {
            "auth_setup": _td_command(td_path, data_dir, "auth setup"),
            "auth_login": _td_command(td_path, data_dir, "auth login"),
            "auth_status": _td_command(td_path, data_dir, "auth status"),
            "init": _td_command(td_path, data_dir, "init <local-root> --create-channel"),
        },
    }
    return report


def run_health_check(td, td_path: str, td_source: str, data_dir: str) -> Dict[str, Any]:
    """Run non-interactive checks without ever requesting or printing secrets."""
    data_dir = os.path.abspath(os.path.expanduser(data_dir))
    report: Dict[str, Any] = {
        "ready": False,
        "setup_guide": SETUP_GUIDE_URL,
        "td": {"path": td_path, "source": td_source},
        "data_directory": data_dir,
        "checks": {},
    }
    checks = report["checks"]

    try:
        version = td.version()
        report["td"]["version"] = version.get("version", "unknown")
        checks["binary"] = _check(
            "pass", f"td is available ({report['td']['version']})"
        )
    except Exception as exc:
        checks["binary"] = _check(
            "fail",
            "td could not be executed",
            f"Check the configured path and executable permissions: {exc}",
        )
        return report

    if os.path.isdir(data_dir) and os.access(data_dir, os.W_OK):
        checks["data_directory"] = _check("pass", "data directory exists and is writable")
    else:
        checks["data_directory"] = _check(
            "warn",
            "data directory is missing or not writable",
            "Create a persistent, container-visible directory and set TD Data Directory "
            "to its path. Do not use a NAS host path inside the container.",
        )

    try:
        auth = td.auth_status()
    except Exception as exc:
        checks["authentication"] = _check(
            "fail",
            "Telegram authentication is not configured",
            "From the Stash container terminal, run the setup commands in "
            f"{SETUP_GUIDE_URL}. The plugin never accepts Telegram codes in Stash.",
        )
        report["error"] = str(exc)
        return report

    if not auth.get("authenticated"):
        setup_cmd = _td_command(td_path, data_dir, "auth setup")
        login_cmd = _td_command(td_path, data_dir, "auth login")
        checks["authentication"] = _check(
            "fail",
            "Telegram login is required",
            f"Run these once in the Stash container terminal: {setup_cmd}; then {login_cmd}",
        )
        return report

    checks["authentication"] = _check(
        "pass", "Telegram session is available (credentials are not displayed)"
    )

    try:
        doctor = td.doctor()
        doctor_checks = doctor.get("checks", {})
        failed = [name for name, status in doctor_checks.items() if status == "fail"]
        if failed:
            checks["telegram"] = _check(
                "fail",
                "td doctor found failed checks",
                "Review the td doctor details in the container terminal and follow "
                f"{SETUP_GUIDE_URL}",
            )
        else:
            checks["telegram"] = _check("pass", "td doctor checks passed")
        if "max_upload_bytes" in doctor:
            report["max_upload_bytes"] = doctor["max_upload_bytes"]
    except Exception as exc:
        checks["telegram"] = _check("warn", "td doctor could not complete", str(exc))

    try:
        drive = td.status()
        report["drive"] = drive
        checks["drive"] = _check("pass", "a td drive root is initialized")
    except Exception as exc:
        checks["drive"] = _check(
            "fail",
            "no usable td drive root was found",
            f"Initialize or bind a Telegram channel from the container terminal with "
            f"`td init`. Details: {exc}",
        )

    report["ready"] = all(
        item.get("status") == "pass"
        for item in checks.values()
        if item.get("status") != "warn"
    ) and checks["data_directory"]["status"] == "pass"
    return report
