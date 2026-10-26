"""
Client wrapper for tg-drive (td) binary.
Communicates via stdin/stdout JSON contract and maps exit codes/errors to Python exceptions.

Contract notes (docs/contracts/cli-contract.md of tg-drive):
- uploads use `td cp <local> <remote-path> [--replace --confirm ...]`
- downloads use `td get <remote-path> <local-dest> [--recursive]`
  (`td cp` is upload-only; using it for downloads would upload instead)
- `td get <dir> <dest> --recursive` places the remote directory's CONTENTS
  under dest (the remote dir name is not recreated)

Binary discovery lives in td_resolver; this class only executes a path it is
given. It also owns the canonical td invocation shape (session flags and
ordering), which setup_check reuses for the copy-paste setup instructions.
"""
import json
import os
import re
import shlex
import subprocess
from typing import Any, Dict, List, Optional, Sequence, Tuple


def parse_td_version(version: str) -> Optional[Tuple[int, int, int]]:
    """
    Parse a td version string ("0.2.1", "v0.2.0") into a comparable tuple.

    Returns None for non-release builds ("dev", "unknown"): those are built
    from source that already contains the features the version gate guards,
    so callers treat them as pass-with-note rather than fail.
    """
    if not version:
        return None
    m = re.match(r"^v?(\d+)\.(\d+)(?:\.(\d+))?", version.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


class TDError(Exception):
    def __init__(self, message: str, code: Optional[str] = None, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class TDRateLimitedError(TDError):
    def __init__(self, message: str, retry_after_seconds: int, details: Optional[Dict[str, Any]] = None):
        super().__init__(message, code="ERR_TELEGRAM_RATE_LIMITED", details=details)
        self.retry_after_seconds = retry_after_seconds


class TDLockedError(TDError):
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message, code="ERR_OPERATION_LOCKED", details=details)


class TDNotFoundError(TDError):
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message, code="ERR_REMOTE_NOT_FOUND", details=details)


class TDClient:
    def __init__(
        self,
        td_path: str,
        data_dir: Optional[str] = None,
        channel: Optional[str] = None,
    ):
        self.td_path = td_path
        if data_dir:
            self.data_dir: Optional[str] = os.path.abspath(os.path.expanduser(data_dir))
            self.config_path: Optional[str] = os.path.join(self.data_dir, "config.toml")
            self.session_path: Optional[str] = os.path.join(self.data_dir, "session.json")
            self.db_path: Optional[str] = os.path.join(self.data_dir, "local_cache.db")
        else:
            self.data_dir = None
            self.config_path = None
            self.session_path = None
            self.db_path = None
        self.channel = channel

    # ------------------------------------------------- command construction

    def _base_cmd(self, subcmd: str, args: Sequence[str]) -> List[str]:
        cmd = [self.td_path, subcmd]
        if self.config_path:
            cmd.extend(["--config", self.config_path])
        if self.session_path:
            cmd.extend(["--session", self.session_path])
        if self.channel:
            cmd.extend(["--channel", self.channel])
        if self.db_path:
            cmd.extend(["--db", self.db_path])
        cmd.extend(args)
        return cmd

    def _build_cmd(self, subcmd: str, args: List[str]) -> List[str]:
        return self._base_cmd(subcmd, args) + ["--json"]

    def command_line(self, subcmd: str, *args: str) -> str:
        """Copy-pasteable shell form of a td invocation (without --json)."""
        return " ".join(shlex.quote(part) for part in self._base_cmd(subcmd, args))

    # ------------------------------------------------------------- td calls

    def version(self) -> Dict[str, Any]:
        """Return td version information."""
        return self._exec(self._build_cmd("version", []))

    def auth_status(self) -> Dict[str, Any]:
        """Return Telegram authentication status."""
        return self._exec(self._build_cmd("auth", ["status"]))

    def doctor(self) -> Dict[str, Any]:
        """Run td's local and Telegram capability checks."""
        return self._exec(self._build_cmd("doctor", []))

    def status(self) -> Dict[str, Any]:
        """Return the configured drive index status."""
        return self._exec(self._build_cmd("status", []))

    def _exec(self, cmd: List[str]) -> Dict[str, Any]:
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            raise TDError(f"td binary not found at '{self.td_path}'")

        stdout = res.stdout.strip()
        stderr = res.stderr.strip()

        if not stdout:
            raise TDError(f"td returned empty stdout (exit {res.returncode}): {stderr}")

        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError as e:
            raise TDError(f"Failed to parse td JSON output (exit {res.returncode}): {stdout[:200]}") from e

        if not envelope.get("ok"):
            err = envelope.get("error", {})
            code = err.get("code", "ERR_UNKNOWN")
            msg = err.get("message", f"td command failed with exit code {res.returncode}")
            details = err.get("details", {})

            if code == "ERR_TELEGRAM_RATE_LIMITED":
                retry_after = details.get("retry_after_seconds", 30)
                raise TDRateLimitedError(msg, retry_after, details)
            elif code == "ERR_OPERATION_LOCKED":
                raise TDLockedError(msg, details)
            elif code == "ERR_REMOTE_NOT_FOUND":
                raise TDNotFoundError(msg, details)
            else:
                raise TDError(msg, code, details)

        return envelope.get("data", {})

    def upload_file(
        self,
        local_path: str,
        remote_path: str,
        as_kind: Optional[str] = None,
        duration: Optional[float] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        streaming: bool = True,
        thumb_path: Optional[str] = None,
        replace: bool = False,
    ) -> Dict[str, Any]:
        """Upload single file with presentation attributes (td cp)."""
        args = [local_path, remote_path]
        if as_kind:
            args.extend(["--as", as_kind])
        if duration is not None:
            args.extend(["--duration", str(duration)])
        if width is not None:
            args.extend(["--width", str(width)])
        if height is not None:
            args.extend(["--height", str(height)])
        if streaming and as_kind == "video":
            args.append("--streaming")
        if thumb_path and os.path.exists(thumb_path):
            args.extend(["--thumb", thumb_path])
        if replace:
            args.extend(["--replace", "--confirm"])

        # --wait makes td sleep through server FLOOD_WAITs and retry the
        # send in-process (budget: rate_limit.max_wait_seconds, 300s by
        # default) instead of failing fast. Uploads are the one call where
        # waiting is always right: re-running the command loses td's
        # in-memory pacing state and re-spawns the upload session.
        args.append("--wait")

        cmd = self._build_cmd("cp", args)
        return self._exec(cmd)

    def list_dir(self, remote_path: str) -> List[Dict[str, Any]]:
        """List remote directory entries: {name, path, type, size, hash, status}."""
        cmd = self._build_cmd("ls", [remote_path])
        data = self._exec(cmd)
        if isinstance(data, list):
            return data
        return data.get("entries", [])

    def tree(self, remote_path: str = "/", depth: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Return the whole nested remote tree in one index-only call.

        Entries are {type, name, path, children}. File entries carry only the
        path here; per-file size and hash come from list_dir, whose file
        entries always carry the stored BLAKE3 hash (td json contract).
        """
        args = [remote_path]
        if depth is not None:
            args.extend(["--depth", str(depth)])
        data = self._exec(self._build_cmd("tree", args))
        if isinstance(data, dict):
            return data.get("tree", [])
        return data if isinstance(data, list) else []

    def upload_album(
        self,
        local_paths: List[str],
        remote_dir: str,
        as_kind: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Publish >=2 local files as native Telegram media groups (td cp
        multi-file form): automatic split at 10 members per group, one human
        caption on the first member, one td-album:v1 inventory per group.

        remote_dir must end with "/" (or be "/") so td treats it as a
        directory; members are published at remote_dir/<local-basename>.
        --replace is not supported in this form (ERR_USAGE). Response shape:
        {uploaded, skipped, errors, albums: [{grouped_id, reply_message_id,
        message_ids, paths}], channel_id, invite_link?}; message_ids align
        with paths positionally.
        """
        if len(local_paths) < 2:
            raise TDError("album upload requires at least two local files")
        if not remote_dir.endswith("/"):
            remote_dir += "/"
        args = list(local_paths) + [remote_dir]
        if as_kind:
            args.extend(["--as", as_kind])
        # --wait absorbs Telegram flood waits in-process, same as single
        # uploads; presentation flags apply uniformly to every group member.
        args.append("--wait")
        cmd = self._build_cmd("cp", args)
        return self._exec(cmd)

    def scan(self, full: bool = True) -> Dict[str, Any]:
        """Trigger td scan to rebuild or update index from Telegram."""
        args = ["--full"] if full else []
        cmd = self._build_cmd("scan", args)
        return self._exec(cmd)

    def remove(self, remote_path: str, confirm: bool = True) -> Dict[str, Any]:
        """
        Delete a remote file from the drive index (td rm). Albums cannot
        --replace, so removing an album member slot before re-uploading it
        is the scan-safe update path (probe-verified: a fresh cp at the
        freed path keeps the album index valid after scan --full).
        """
        args = [remote_path]
        if confirm:
            args.append("--confirm")
        cmd = self._build_cmd("rm", args)
        return self._exec(cmd)

    def adopt(
        self,
        message_id: Optional[int] = None,
        remote_path: Optional[str] = None,
        unmanaged: bool = False,
        into: str = "/",
        with_hash: bool = True,
        confirm: bool = True,
    ) -> Dict[str, Any]:
        """
        Adopt existing channel messages into the td index (ADR 0019; moves
        no bytes). --unmanaged claims every unmanaged media/text message
        into a remote directory prefix; with_hash downloads each file to
        compute its BLAKE3 content hash so later downloads verify.
        """
        args = []
        if message_id is not None:
            args.append(str(message_id))
        if remote_path is not None:
            args.append(remote_path)
        if unmanaged:
            args.append("--unmanaged")
        if into:
            args.extend(["--into", into])
        if with_hash:
            args.append("--hash")
        if confirm:
            args.append("--confirm")
        cmd = self._build_cmd("adopt", args)
        return self._exec(cmd)

    def download(
        self,
        remote_path: str,
        local_dest: str,
        recursive: bool = False,
        skip_existing: bool = False,
        continue_on_error: bool = False,
    ) -> Dict[str, Any]:
        """
        Download a remote file or directory (td get).

        For a file: local_dest is the exact destination path (or an existing
        directory, in which case the remote basename is appended).
        For recursive directories: the remote directory's contents are placed
        under local_dest.
        """
        args = [remote_path, local_dest]
        if recursive:
            args.append("--recursive")
        if skip_existing:
            args.append("--skip-existing")
        if continue_on_error:
            args.append("--continue-on-error")
        cmd = self._build_cmd("get", args)
        return self._exec(cmd)
