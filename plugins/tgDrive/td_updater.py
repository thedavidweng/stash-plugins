"""Explicit, checksum-verified td release installer.

This module is only called by the user-facing update task. Backup and restore
never download or replace the td core as a side effect.
"""
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import tempfile
from typing import Dict, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from td_resolver import _extract_archive


REPOSITORY = "thedavidweng/tg-drive-cli"
RELEASES_URL = f"https://api.github.com/repos/{REPOSITORY}/releases"


def _platform_asset() -> Tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine)
    if system == "darwin":
        return "td_darwin_universal.tar.gz", "tar.gz"
    if system == "linux" and arch:
        return f"td_linux_{arch}.tar.gz", "tar.gz"
    if system == "windows" and arch:
        return f"td_windows_{arch}.zip", "zip"
    raise ValueError(
        f"no published td release matches {platform.system()} {platform.machine()}"
    )


def _read_url(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "stash-tgdrive-plugin"})
    try:
        with urlopen(request, timeout=60) as response:
            return response.read()
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"could not download {url}: {exc}") from exc


def _resolve_version(version: str) -> str:
    if version and version != "latest":
        return version
    payload = json.loads(_read_url(f"{RELEASES_URL}/latest").decode("utf-8"))
    tag = payload.get("tag_name")
    if not tag:
        raise RuntimeError("GitHub returned a release without tag_name")
    return str(tag)


def _checksum(checksums: bytes, asset: str) -> str:
    for raw_line in checksums.decode("utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) >= 2 and os.path.basename(fields[-1].lstrip("*")) == asset:
            digest = fields[0].lower()
            if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
                return digest
    raise ValueError(f"checksums.txt does not contain a SHA-256 entry for {asset}")


def _verify_sha256(content: bytes, expected: str, asset: str) -> None:
    actual = hashlib.sha256(content).hexdigest()
    if actual != expected:
        raise ValueError(
            f"SHA-256 mismatch for {asset}: expected {expected}, got {actual}"
        )


def _verify_binary(path: str) -> Dict[str, str]:
    if os.name != "nt":
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    result = subprocess.run(
        [path, "version", "--json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"installed td failed version check (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("installed td returned invalid JSON from version check") from exc
    if not envelope.get("ok"):
        raise RuntimeError("installed td version check returned an error")
    data = envelope.get("data") or {}
    return {"version": str(data.get("version", "unknown"))}


def install_td_core(plugin_dir: str, version: str = "latest") -> Dict[str, str]:
    """Install the matching release into ``plugin_dir/bin`` atomically."""
    plugin_dir = os.path.abspath(plugin_dir)
    resolved_version = _resolve_version(version)
    asset, _ = _platform_asset()
    base_url = (
        f"https://github.com/{REPOSITORY}/releases/download/"
        f"{resolved_version}"
    )
    checksums = _read_url(f"{base_url}/checksums.txt")
    archive = _read_url(f"{base_url}/{asset}")
    _verify_sha256(archive, _checksum(checksums, asset), asset)

    with tempfile.TemporaryDirectory(prefix="td-update-", dir=plugin_dir) as staging:
        archive_path = os.path.join(staging, asset)
        with open(archive_path, "wb") as handle:
            handle.write(archive)
        extracted_dir = os.path.join(staging, "extracted")
        extracted = _extract_archive(archive_path, extracted_dir)

        target_dir = os.path.join(plugin_dir, "bin")
        os.makedirs(target_dir, exist_ok=True)
        target_name = "td.exe" if os.name == "nt" else "td"
        temporary_target = os.path.join(target_dir, f".{target_name}.new")
        final_target = os.path.join(target_dir, target_name)
        shutil.copyfile(extracted, temporary_target)
        if os.name != "nt":
            os.chmod(temporary_target, 0o755)
        os.replace(temporary_target, final_target)

    version_data = _verify_binary(final_target)
    return {
        "status": "installed",
        "version": resolved_version,
        "binary_version": version_data["version"],
        "asset": asset,
        "path": final_target,
    }
