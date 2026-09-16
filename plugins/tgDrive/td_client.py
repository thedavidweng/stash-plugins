"""
Client wrapper for tg-drive-cli (td) binary.
Communicates via stdin/stdout JSON contract and maps exit codes/errors to Python exceptions.

Contract notes (docs/contracts/cli-contract.md of tg-drive-cli):
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
import shlex
import subprocess
from typing import Any, Dict, List, Optional, Sequence


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

    def scan(self, full: bool = True) -> Dict[str, Any]:
        """Trigger td scan to rebuild or update index from Telegram."""
        args = ["--full"] if full else []
        cmd = self._build_cmd("scan", args)
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
