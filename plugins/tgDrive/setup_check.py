"""User-facing setup and health reporting for the Stash task UI."""
import os
from typing import Any, Dict, Optional

from td_client import TDClient


SETUP_GUIDE_URL = (
    "https://github.com/thedavidweng/stash-plugins/blob/main/"
    "plugins/tgDrive/SETUP.md"
)


def _check(status: str, message: str, hint: str = "") -> Dict[str, str]:
    result = {"status": status, "message": message}
    if hint:
        result["hint"] = hint
    return result


def build_setup_report(
    td: TDClient,
    td_source: str = "not resolved",
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """Return safe, copy-friendly setup information for the Stash UI."""
    report: Dict[str, Any] = {
        "status": "setup_required",
        "ready": False,
        "setup_guide": SETUP_GUIDE_URL,
        "td": {"path": td.td_path, "source": td_source},
        "data_directory": td.data_dir,
        "message": (
            "Complete the one-time td setup in the Stash container terminal. "
            "Do not enter Telegram API hashes, login codes, or 2FA passwords "
            "in Stash plugin settings."
        ),
        "commands": {
            "auth_setup": td.command_line("auth", "setup"),
            "auth_login": td.command_line("auth", "login"),
            "auth_status": td.command_line("auth", "status"),
            "init": td.command_line("init", "<local-root>", "--create-channel"),
        },
    }
    if error:
        report["td"]["error"] = error
    return report


def run_health_check(td: TDClient, td_source: str) -> Dict[str, Any]:
    """Run non-interactive checks without ever requesting or printing secrets."""
    report: Dict[str, Any] = {
        "ready": False,
        "setup_guide": SETUP_GUIDE_URL,
        "td": {"path": td.td_path, "source": td_source},
        "data_directory": td.data_dir,
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

    if td.data_dir and os.path.isdir(td.data_dir) and os.access(td.data_dir, os.W_OK):
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
        setup_cmd = td.command_line("auth", "setup")
        login_cmd = td.command_line("auth", "login")
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

    # Readiness: binary, data directory, login and drive must pass; td doctor
    # warnings are tolerated but doctor failures are not.
    report["ready"] = (
        checks["binary"]["status"] == "pass"
        and checks["data_directory"]["status"] == "pass"
        and checks["authentication"]["status"] == "pass"
        and checks["telegram"]["status"] != "fail"
        and checks["drive"]["status"] == "pass"
    )
    return report
