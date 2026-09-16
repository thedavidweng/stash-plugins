"""Resolve a td executable from plugin settings or the plugin directory.

The resolver deliberately supports only local files. Downloading and
executing a release is an explicit setup action, not a side effect of a
backup task. Release archives are extracted into a private runtime directory
and never into the published plugin source tree.
"""
import os
import shutil
import stat
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class TDResolution:
    path: str
    source: str


def _is_archive(path: str) -> bool:
    lowered = path.lower()
    return lowered.endswith((".tar.gz", ".tgz", ".zip"))


def _executable_name() -> str:
    return "td.exe" if os.name == "nt" else "td"


def _acceptable_executable_names() -> tuple:
    preferred = _executable_name()
    alternate = "td" if preferred == "td.exe" else "td.exe"
    return preferred, alternate


def _safe_member_path(root: str, name: str) -> str:
    destination = os.path.realpath(os.path.join(root, name))
    root_real = os.path.realpath(root)
    if os.path.commonpath((root_real, destination)) != root_real:
        raise ValueError("release archive contains an unsafe path")
    return destination


def _archive_member_names(archive_path: str) -> Iterable[str]:
    names = _acceptable_executable_names()
    if archive_path.lower().endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive_path, "r:*") as bundle:
            for member in bundle.getmembers():
                if member.isfile() and os.path.basename(member.name) in names:
                    yield member.name
        return

    with zipfile.ZipFile(archive_path) as bundle:
        for member in bundle.infolist():
            if not member.is_dir() and os.path.basename(member.filename) in names:
                yield member.filename


def extract_td_archive(archive_path: str, runtime_dir: str) -> str:
    """Extract the td executable from a release archive into runtime_dir.

    Returns the installed executable path. Rejects unsafe member paths and
    symlink members; installs atomically (staged copy + os.replace).
    """
    os.makedirs(runtime_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="td-extract-", dir=runtime_dir) as staging:
        members = list(_archive_member_names(archive_path))
        if not members:
            raise ValueError(
                f"release archive does not contain td or td.exe: {archive_path}"
            )
        preferred = _executable_name()
        member = next(
            (candidate for candidate in members if os.path.basename(candidate) == preferred),
            members[0],
        )
        executable = os.path.basename(member)
        staged_path = _safe_member_path(staging, member)

        if archive_path.lower().endswith((".tar.gz", ".tgz")):
            with tarfile.open(archive_path, "r:*") as bundle:
                info = bundle.getmember(member)
                if info.issym() or info.islnk():
                    raise ValueError("release archive contains a symbolic link")
                extracted = bundle.extractfile(info)
                if extracted is None:
                    raise ValueError("could not read td from release archive")
                os.makedirs(os.path.dirname(staged_path), exist_ok=True)
                with open(staged_path, "wb") as output:
                    shutil.copyfileobj(extracted, output)
        else:
            with zipfile.ZipFile(archive_path) as bundle:
                info = bundle.getinfo(member)
                # Zip files can encode symlinks in Unix permission bits.
                mode = (info.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK:
                    raise ValueError("release archive contains a symbolic link")
                os.makedirs(os.path.dirname(staged_path), exist_ok=True)
                with bundle.open(info) as source, open(staged_path, "wb") as output:
                    shutil.copyfileobj(source, output)

        installed = os.path.join(runtime_dir, executable)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{executable}.",
            suffix=".new",
            dir=runtime_dir,
        )
        os.close(fd)
        try:
            shutil.copyfile(staged_path, temporary)
            if os.name != "nt":
                os.chmod(temporary, 0o755)
            os.replace(temporary, installed)
            return installed
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _resolve_configured(path: str, runtime_dir: str) -> TDResolution:
    candidate = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(candidate):
        raise ValueError(f"configured td path does not exist: {path}")
    if _is_archive(candidate):
        return TDResolution(extract_td_archive(candidate, runtime_dir), "configured release archive")
    if os.name != "nt" and not os.access(candidate, os.X_OK):
        try:
            os.chmod(candidate, os.stat(candidate).st_mode | stat.S_IXUSR)
        except OSError as exc:
            raise ValueError(f"configured td binary is not executable: {path}") from exc
    return TDResolution(candidate, "configured binary")


def resolve_td_path(
    configured_path: Optional[str],
    plugin_dir: str,
    runtime_dir: Optional[str] = None,
) -> TDResolution:
    """Resolve a local td binary or release archive.

    Precedence is explicit setting, plugin ``bin/td``, plugin ``td``, a
    matching archive in the plugin directory, then PATH. A configured path
    is never silently ignored when it is invalid.
    """
    plugin_dir = os.path.abspath(plugin_dir)
    runtime_dir = runtime_dir or os.path.join(plugin_dir, ".td-runtime")
    if configured_path and configured_path.strip():
        return _resolve_configured(configured_path.strip(), runtime_dir)

    executable = _executable_name()
    for candidate in (
        os.path.join(plugin_dir, "bin", executable),
        os.path.join(plugin_dir, executable),
    ):
        if os.path.isfile(candidate) and (os.name == "nt" or os.access(candidate, os.X_OK)):
            return TDResolution(candidate, "plugin directory")

    archive_names = sorted(
        name
        for name in os.listdir(plugin_dir)
        if name.lower().endswith((".tar.gz", ".tgz", ".zip"))
        and ("td" in name.lower() or "tg-drive" in name.lower())
    )
    if archive_names:
        archive = os.path.join(plugin_dir, archive_names[0])
        return TDResolution(extract_td_archive(archive, runtime_dir), "plugin directory archive")

    found = shutil.which("td")
    if found:
        return TDResolution(found, "system PATH")

    raise ValueError(
        "td was not found. Set TD Core Path to a td binary or release archive, "
        "place td in the plugin bin directory, or install td in PATH."
    )
