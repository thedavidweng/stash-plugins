"""
Stash GraphQL API client.

GraphQL is the only access path to Stash state. This module never reads or
writes Stash's SQLite database or config files directly:

- scene enumeration: findScenes
- plugin settings: configuration.plugins (settings are NOT passed to raw
  plugins on stdin; they are stored in Stash's own config)
- config file location: configuration.general.configFilePath
  (the raw config.yml is then uploaded verbatim - Stash exposes no GraphQL
  for its raw config bytes)
- metadata export: exportObjects (returns a download link for a zip that is
  the native Stash JSON export, importable via importObjects)
- database backup: backupDatabase with download:true (a consistent snapshot
  produced by Stash itself; includeBlobs requires Stash >= 0.31)
- metadata import: importObjects (GraphQL multipart file upload)
- library scan: metadataScan + findJob polling

Requires Stash >= 0.28 (exportObjects / importObjects / backupDatabase).
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

# Stash version that added BackupDatabaseInput.includeBlobs.
INCLUDE_BLOBS_MIN_VERSION = (0, 31)

ACTIVE_JOB_STATUSES = ("READY", "RUNNING")


def parse_version(version: str) -> Optional[Tuple[int, int]]:
    """Parse a Stash version string like "v0.31.1-167-gb872e360" into (major, minor)."""
    if not version:
        return None
    m = re.search(r"v?(\d+)\.(\d+)", version)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))


class StashClient:
    def __init__(
        self,
        base_url: str = "http://localhost:9999",
        api_key: Optional[str] = None,
        session_cookie: Optional[Any] = None,
        timeout: int = 120,
        long_timeout: int = 1800,
    ):
        self.base_url = base_url.rstrip("/")
        self.graphql_url = f"{self.base_url}/graphql"
        self.api_key = api_key
        self.session_cookie = session_cookie
        self.timeout = timeout
        # exportObjects and backupDatabase run synchronously inside the
        # GraphQL call and can take minutes on large libraries.
        self.long_timeout = long_timeout
        self._version_cache: Optional[str] = None

    # ------------------------------------------------------------------ core

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["ApiKey"] = self.api_key
        if self.session_cookie:
            if isinstance(self.session_cookie, dict):
                cookie_val = self.session_cookie.get("Value", "")
            else:
                cookie_val = str(self.session_cookie)
            if cookie_val:
                headers["Cookie"] = f"session={cookie_val}"
        return headers

    def _post(self, body: bytes, headers: Dict[str, str], timeout: int) -> Dict[str, Any]:
        """POST a GraphQL request and return the parsed JSON envelope."""
        req = urllib.request.Request(self.graphql_url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Stash GraphQL HTTP {e.code}: {err_body}") from e
        except Exception as e:
            raise RuntimeError(f"Stash GraphQL connection error: {e}") from e

    def _parse_graphql(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Unwrap a GraphQL envelope, raising on transport-level errors."""
        if data.get("errors"):
            msg = data["errors"][0].get("message", "Unknown GraphQL error")
            raise RuntimeError(f"Stash GraphQL error: {msg}")
        return data.get("data", {})

    def execute(self, query: str, variables: Optional[Dict[str, Any]] = None, timeout: Optional[int] = None) -> Dict[str, Any]:
        payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        return self._parse_graphql(self._post(payload, self._headers(), timeout or self.timeout))

    # ---------------------------------------------------------------- scenes

    def find_scenes(self, page: int = 1, per_page: int = 50) -> Dict[str, Any]:
        q = """
        query($filter: FindFilterType!) {
            findScenes(filter: $filter) {
                count
                scenes {
                    id
                    title
                    code
                    date
                    studio {
                        name
                    }
                    performers {
                        name
                    }
                    tags {
                        name
                    }
                    paths {
                        screenshot
                    }
                    files {
                        id
                        path
                        size
                        duration
                        width
                        height
                    }
                }
            }
        }
        """
        variables = {
            "filter": {
                "page": page,
                "per_page": per_page,
                "sort": "updated_at",
                "direction": "ASC",
            }
        }
        res = self.execute(q, variables)
        return res.get("findScenes", {"count": 0, "scenes": []})

    def download_image(self, url_or_path: str, dest_path: str) -> bool:
        """Download a scene screenshot from the Stash server."""
        if url_or_path.startswith("http://") or url_or_path.startswith("https://"):
            fetch_url = url_or_path
        else:
            fetch_url = f"{self.base_url}{url_or_path}"

        req = urllib.request.Request(fetch_url, headers=self._headers(), method="GET")
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp, open(dest_path, "wb") as f:
                f.write(resp.read())
            return True
        except Exception:
            return False

    # ------------------------------------------------- system / configuration

    def get_version(self) -> str:
        if self._version_cache is None:
            try:
                res = self.execute("{ version { version } }")
                self._version_cache = res.get("version", {}).get("version", "unknown")
            except Exception:
                self._version_cache = "unknown"
        return self._version_cache

    def version_tuple(self) -> Optional[Tuple[int, int]]:
        return parse_version(self.get_version())

    def supports_include_blobs(self) -> bool:
        """BackupDatabaseInput.includeBlobs exists only on Stash >= 0.31."""
        v = self.version_tuple()
        return v is not None and v >= INCLUDE_BLOBS_MIN_VERSION

    def get_config_file_path(self) -> Optional[str]:
        """Absolute path of Stash's config.yml, read via GraphQL."""
        try:
            res = self.execute("{ configuration { general { configFilePath } } }")
            path = res.get("configuration", {}).get("general", {}).get("configFilePath")
            if path:
                return path
        except Exception:
            pass
        try:
            res = self.execute("{ systemStatus { configPath } }")
            return res.get("systemStatus", {}).get("configPath")
        except Exception:
            return None

    def get_plugin_settings(self, plugin_id: str) -> Dict[str, Any]:
        """
        Read this plugin's settings from Stash's configuration.

        Settings edited in Settings -> Plugins are stored in Stash's config
        under `plugins.<id>` and are NOT forwarded to raw plugins on stdin.
        """
        try:
            res = self.execute("{ configuration { plugins } }")
            plugins = res.get("configuration", {}).get("plugins", {}) or {}
            settings = plugins.get(plugin_id) or {}
            return settings if isinstance(settings, dict) else {}
        except Exception:
            return {}

    # ------------------------------------------------------- backup artifacts

    def export_objects(self, include_dependencies: bool = True, timeout: Optional[int] = None) -> Optional[str]:
        """
        Run a native Stash JSON export synchronously and return the download
        URL of the resulting zip. The zip contains the full metadata export
        (scenes with markers, ratings and performer/tag/studio associations,
        plus files with fingerprints) and is exactly what importObjects
        accepts on restore.
        """
        q = """
        mutation($input: ExportObjectsInput!) {
            exportObjects(input: $input)
        }
        """
        variables = {
            "input": {
                "scenes": {"all": True},
                "performers": {"all": True},
                "studios": {"all": True},
                "tags": {"all": True},
                "groups": {"all": True},
                "includeDependencies": include_dependencies,
            }
        }
        res = self.execute(q, variables, timeout=timeout or self.long_timeout)
        return res.get("exportObjects")

    def backup_database(self, include_blobs: bool = False, timeout: Optional[int] = None) -> Optional[str]:
        """
        Ask Stash to produce a consistent database snapshot and return the
        download URL. includeBlobs is only sent when the server supports it.
        """
        q = """
        mutation($input: BackupDatabaseInput!) {
            backupDatabase(input: $input)
        }
        """
        input_obj: Dict[str, Any] = {"download": True}
        if include_blobs and self.supports_include_blobs():
            input_obj["includeBlobs"] = True
        res = self.execute(q, {"input": input_obj}, timeout=timeout or self.long_timeout)
        return res.get("backupDatabase")

    def download_file(self, url: str, dest_path: str, timeout: Optional[int] = None) -> str:
        """
        Download a Stash download link (returned by exportObjects /
        backupDatabase) to dest_path and return the local path.

        Stash serves these from its download store and deletes them shortly
        after the first fetch, so failures are not retried here.
        """
        if url.startswith("/"):
            url = f"{self.base_url}{url}"
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp, open(dest_path, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
        except Exception as e:
            raise RuntimeError(f"failed to download {url}: {e}") from e
        return dest_path

    # ------------------------------------------------------- restore / jobs

    def import_objects(
        self,
        file_path: str,
        duplicate_behaviour: str = "OVERWRITE",
        missing_ref_behaviour: str = "CREATE",
        timeout: Optional[int] = None,
    ) -> str:
        """
        Upload a Stash export zip via GraphQL multipart (ImportObjectsInput
        takes an Upload scalar) and return the import job ID.
        duplicate/missingRef behaviours: IGNORE / OVERWRITE / FAIL and
        IGNORE / FAIL / CREATE.
        """
        query = (
            "mutation($file: Upload!) {"
            f" importObjects(input: {{file: $file,"
            f" duplicateBehaviour: {duplicate_behaviour},"
            f" missingRefBehaviour: {missing_ref_behaviour}}})"
            " }"
        )
        operations = {"query": query, "variables": {"file": None}}
        files_map = {"0": ["variables.file"]}

        boundary = "----tgDriveBoundary" + uuid.uuid4().hex
        crlf = b"\r\n"
        parts: List[bytes] = []

        def add_field(name: str, value: bytes) -> None:
            parts.append(f"--{boundary}".encode())
            parts.append(f'Content-Disposition: form-data; name="{name}"'.encode())
            parts.append(b"")
            parts.append(value)

        add_field("operations", json.dumps(operations).encode())
        add_field("map", json.dumps(files_map).encode())
        # file field
        parts.append(f"--{boundary}".encode())
        parts.append(
            f'Content-Disposition: form-data; name="0"; filename="{os.path.basename(file_path)}"'.encode()
        )
        parts.append(b"Content-Type: application/zip")
        parts.append(b"")
        with open(file_path, "rb") as f:
            parts.append(f.read())
        # closing boundary
        parts.append(f"--{boundary}--".encode())

        headers = self._headers()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"

        data = self._parse_graphql(
            self._post(crlf.join(parts), headers, timeout or self.long_timeout)
        )

        job_id = data.get("importObjects")
        if not job_id:
            raise RuntimeError("Stash did not return an import job ID")
        return str(job_id)

    def trigger_scan(self, paths: Optional[List[str]] = None) -> str:
        """Start a Stash library scan and return the job ID."""
        q = """
        mutation($input: ScanMetadataInput!) {
            metadataScan(input: $input)
        }
        """
        res = self.execute(q, {"input": {"paths": paths or []}})
        job_id = res.get("metadataScan")
        if not job_id:
            raise RuntimeError("Stash did not return a scan job ID")
        return str(job_id)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        q = """
        query($id: FindJobInput!) {
            findJob(input: $id) {
                id
                status
                description
                progress
                error
            }
        }
        """
        res = self.execute(q, {"id": {"id": str(job_id)}})
        return res.get("findJob")

    def wait_for_job(
        self,
        job_id: str,
        timeout_seconds: int,
        poll_interval: float = 5.0,
        progress=None,
    ) -> str:
        """
        Poll a Stash job until it leaves the queue. Returns the final job
        status (FINISHED / FAILED / CANCELLED / TIMEOUT / UNKNOWN).
        """
        deadline = time.monotonic() + max(timeout_seconds, 1)
        while True:
            job = self.get_job(job_id)
            if job is None:
                return "UNKNOWN"
            status = job.get("status")
            if progress is not None:
                try:
                    p = job.get("progress")
                    if isinstance(p, (int, float)):
                        progress(float(p))
                except Exception:
                    pass
            if status not in ACTIVE_JOB_STATUSES:
                return str(status)
            if time.monotonic() > deadline:
                return "TIMEOUT"
            time.sleep(poll_interval)
