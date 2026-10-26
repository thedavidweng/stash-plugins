"""
Stub Stash server for the offline end-to-end harness.

Implements exactly the GraphQL and download surface the plugin uses
(stash_client.py is the contract): paged findScenes/findImages/
findGalleries, exportObjects/backupDatabase download links, the image and
screenshot endpoints, metadataScan/findJob/importObjects for restore, and
configuration/version reads. No SQLite, no real Stash: the library is a
plain Python structure built by the test fixture.

Dispatch is by substring on the query text: the plugin's queries are fixed
constants, so a real GraphQL engine would buy nothing here.
"""
import io
import json
import re
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional


class StubStash:
    """A controllable in-memory Stash standing in for the real server."""

    def __init__(
        self,
        scenes: List[Dict[str, Any]],
        images: List[Dict[str, Any]],
        galleries: List[Dict[str, Any]],
        image_bytes: Dict[str, bytes],
        config_file_path: str = "/stub/config.yml",
        version: str = "v0.31.1",
    ):
        self.scenes = scenes
        self.images = images
        self.galleries = galleries
        self.image_bytes = dict(image_bytes)
        self.config_file_path = config_file_path
        self.version = version
        self.plugin_settings: Dict[str, Any] = {}

        # observable call log for assertions
        self.scan_paths: List[List[str]] = []
        self.imports: List[str] = []  # raw multipart bodies
        self.export_calls = 0
        self.backup_calls = 0

        self._download_counter = 0
        self._downloads: Dict[str, bytes] = {}
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status: int, body: bytes, content_type: str):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?")[0]
                if path.startswith("/downloads/"):
                    key = path[len("/downloads/"):]
                    payload = stub._downloads.get(key)
                    if payload is None:
                        self._send(404, b"gone", "application/octet-stream")
                    else:
                        self._send(200, payload, "application/zip")
                    return
                m = re.match(r"^/image/([^/]+)/image$", path)
                if m:
                    payload = stub.image_bytes.get(m.group(1), b"")
                    self._send(200, payload, "image/jpeg")
                    return
                m = re.match(r"^/scene/([^/]+)/screenshot$", path)
                if m:
                    self._send(200, b"\xff\xd8stub-cover\xff\xd9", "image/jpeg")
                    return
                self._send(404, b"not found", "text/plain")

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                content_type = self.headers.get("Content-Type") or ""
                if "multipart/form-data" in content_type:
                    # importObjects: accept the upload without parsing it
                    stub.imports.append(body.decode("utf-8", errors="replace"))
                    self._send(200, json.dumps({
                        "data": {"importObjects": "import-job-1"}
                    }).encode(), "application/json")
                    return
                try:
                    request = json.loads(body.decode("utf-8"))
                except json.JSONDecodeError:
                    self._send(400, b"bad json", "text/plain")
                    return
                data = stub._graphql(request.get("query") or "", request.get("variables") or {})
                self._send(200, json.dumps({"data": data}).encode(), "application/json")

        self._handler = Handler
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"

    # ------------------------------------------------------------- lifecycle

    def start(self) -> "StubStash":
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=10)

    # -------------------------------------------------------------- graphql

    def _paged(self, items: List[Dict[str, Any]], variables: Dict[str, Any]):
        find_filter = (variables or {}).get("filter") or {}
        page = int(find_filter.get("page") or 1)
        per_page = int(find_filter.get("per_page") or 40)
        start = (page - 1) * per_page
        return {"count": len(items), "items": items[start:start + per_page]}

    def _register_download(self, payload: bytes) -> str:
        self._download_counter += 1
        key = f"artifact-{self._download_counter}.zip"
        self._downloads[key] = payload
        return f"/downloads/{key}"

    @staticmethod
    def _zip_bytes(entries: Dict[str, bytes]) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            for name, payload in entries.items():
                bundle.writestr(name, payload)
        return buffer.getvalue()

    def _graphql(self, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        if "findScenes" in query:
            paged = self._paged(self.scenes, variables)
            return {"findScenes": {"count": paged["count"], "scenes": paged["items"]}}
        if "findImages" in query:
            paged = self._paged(self.images, variables)
            return {"findImages": {"count": paged["count"], "images": paged["items"]}}
        if "findGalleries" in query:
            paged = self._paged(self.galleries, variables)
            return {"findGalleries": {"count": paged["count"], "galleries": paged["items"]}}
        if "exportObjects" in query:
            self.export_calls += 1
            return {"exportObjects": self._register_download(self._zip_bytes({
                "scenes/1.json": b'{"title": "E2E Scene"}',
                "images/1.json": b'{"title": "E2E Image"}',
                "galleries/1.json": b'{"title": "E2E Gallery"}',
            }))}
        if "backupDatabase" in query:
            self.backup_calls += 1
            return {"backupDatabase": self._register_download(self._zip_bytes({
                "stash-go.sqlite": b"stub-sqlite-bytes",
            }))}
        if "metadataScan" in query:
            paths = (variables or {}).get("input", {}).get("paths") or []
            self.scan_paths.append(list(paths))
            return {"metadataScan": "scan-job-1"}
        if "findJob" in query:
            return {"findJob": {"id": "1", "status": "FINISHED", "progress": 1.0}}
        if "configFilePath" in query:
            return {"configuration": {"general": {"configFilePath": self.config_file_path}}}
        if "plugins" in query:
            return {"configuration": {"plugins": {**self.plugin_settings}}}
        if "version" in query:
            return {"version": {"version": self.version}}
        return {}
