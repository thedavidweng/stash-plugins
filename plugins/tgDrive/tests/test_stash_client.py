import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stash_client import StashClient, parse_version


class FakeResponse:
    """Context-manager fake for urllib.request.urlopen responses."""

    def __init__(self, payload: bytes):
        self.payload = payload
        self._pos = 0

    def read(self, n=-1):
        if self._pos >= len(self.payload):
            return b""
        size = len(self.payload) if not n or n < 0 else min(n, len(self.payload) - self._pos)
        chunk = self.payload[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def call_variables(mock_exec):
    """Positional arguments of StashClient.execute are (query, variables)."""
    return mock_exec.call_args.args[1]


class TestVersionParsing(unittest.TestCase):
    def test_parse_release_version(self):
        self.assertEqual(parse_version("v0.31.1"), (0, 31))
        self.assertEqual(parse_version("v0.28.0"), (0, 28))
        self.assertEqual(parse_version("v0.31.1-167-gb872e360"), (0, 31))

    def test_parse_garbage(self):
        self.assertIsNone(parse_version("unknown"))
        self.assertIsNone(parse_version(""))
        self.assertIsNone(parse_version(None))


class TestBackupDatabaseInput(unittest.TestCase):
    def setUp(self):
        self.client = StashClient(base_url="http://localhost:9999")

    def _set_version(self, version):
        self.client._version_cache = version

    def test_include_blobs_sent_on_031_plus(self):
        self._set_version("v0.31.1")
        with patch.object(self.client, "execute", return_value={"backupDatabase": "http://x/y.zip"}) as mock_exec:
            self.client.backup_database(include_blobs=True)
        self.assertEqual(call_variables(mock_exec)["input"], {"download": True, "includeBlobs": True})

    def test_include_blobs_omitted_on_030(self):
        self._set_version("v0.30.0")
        with patch.object(self.client, "execute", return_value={"backupDatabase": "http://x/y.zip"}) as mock_exec:
            self.client.backup_database(include_blobs=True)
        self.assertEqual(call_variables(mock_exec)["input"], {"download": True})

    def test_include_blobs_omitted_when_version_unknown(self):
        self._set_version("unknown")
        with patch.object(self.client, "execute", return_value={"backupDatabase": "http://x/y.zip"}) as mock_exec:
            self.client.backup_database(include_blobs=True)
        self.assertEqual(call_variables(mock_exec)["input"], {"download": True})


class TestPluginSettings(unittest.TestCase):
    def test_get_plugin_settings_reads_configuration_map(self):
        client = StashClient(base_url="http://localhost:9999")
        fake_data = {"configuration": {"plugins": {"tgDrive": {"backup_scenes": True, "max_file_size_gb": 4}}}}
        with patch.object(client, "execute", return_value=fake_data) as mock_exec:
            settings = client.get_plugin_settings("tgDrive")
        self.assertEqual(settings, {"backup_scenes": True, "max_file_size_gb": 4})
        self.assertIn("configuration { plugins }", mock_exec.call_args.args[0])

    def test_get_plugin_settings_missing_plugin(self):
        client = StashClient(base_url="http://localhost:9999")
        fake_data = {"configuration": {"plugins": {}}}
        with patch.object(client, "execute", return_value=fake_data):
            self.assertEqual(client.get_plugin_settings("tgDrive"), {})

    def test_get_config_file_path(self):
        client = StashClient(base_url="http://localhost:9999")
        fake_data = {"configuration": {"general": {"configFilePath": "/root/.stash/config.yml"}}}
        with patch.object(client, "execute", return_value=fake_data):
            self.assertEqual(client.get_config_file_path(), "/root/.stash/config.yml")


class TestExportObjects(unittest.TestCase):
    def test_export_input_shape(self):
        client = StashClient(base_url="http://localhost:9999")
        with patch.object(client, "execute", return_value={"exportObjects": "http://x/export.zip"}) as mock_exec:
            url = client.export_objects()
        self.assertEqual(url, "http://x/export.zip")
        inp = call_variables(mock_exec)["input"]
        self.assertEqual(inp["scenes"], {"all": True})
        self.assertEqual(inp["performers"], {"all": True})
        self.assertEqual(inp["studios"], {"all": True})
        self.assertEqual(inp["tags"], {"all": True})
        self.assertEqual(inp["groups"], {"all": True})
        self.assertTrue(inp["includeDependencies"])
        self.assertNotIn("images", inp)
        self.assertNotIn("galleries", inp)


class TestImportObjectsMultipart(unittest.TestCase):
    def test_multipart_body_shape(self):
        client = StashClient(base_url="http://localhost:9999", api_key="KEY123")
        tmp = tempfile.mktemp(suffix=".zip")
        with open(tmp, "wb") as f:
            f.write(b"PK-zip-bytes")

        req_holder = {}

        def fake_urlopen(req, timeout=None):
            req_holder["req"] = req
            return FakeResponse(json.dumps({"data": {"importObjects": "77"}}).encode())

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            job_id = client.import_objects(tmp, duplicate_behaviour="OVERWRITE", missing_ref_behaviour="CREATE")

        self.assertEqual(job_id, "77")
        req = req_holder["req"]
        content_type = req.headers.get("Content-type")
        self.assertIn("multipart/form-data", content_type)
        # API key auth travels with the upload
        self.assertEqual(req.headers.get("Apikey"), "KEY123")

        body = req.data
        boundary = content_type.split("boundary=")[1].encode()
        parts = body.split(b"--" + boundary)

        # operations field carries the mutation with the enum literals
        operations_blob = [p for p in parts if b'name="operations"' in p]
        self.assertTrue(operations_blob, "operations field missing")
        operations = json.loads(operations_blob[0].split(b"\r\n\r\n", 1)[1])
        self.assertIn("importObjects(input: {file: $file", operations["query"])
        self.assertIn("duplicateBehaviour: OVERWRITE", operations["query"])
        self.assertIn("missingRefBehaviour: CREATE", operations["query"])
        self.assertEqual(operations["variables"], {"file": None})

        # map field binds file part 0 to variables.file
        map_blob = [p for p in parts if b'name="map"' in p]
        files_map = json.loads(map_blob[0].split(b"\r\n\r\n", 1)[1])
        self.assertEqual(files_map, {"0": ["variables.file"]})

        # the zip bytes are in the file part
        self.assertIn(b"PK-zip-bytes", body)
        os.unlink(tmp)


class TestJobs(unittest.TestCase):
    def test_wait_for_job_polls_until_finished(self):
        client = StashClient(base_url="http://localhost:9999")
        statuses = [
            {"id": "1", "status": "RUNNING", "progress": 0.5},
            {"id": "1", "status": "FINISHED", "progress": 1.0},
        ]
        with patch.object(client, "get_job", side_effect=statuses):
            status = client.wait_for_job("1", timeout_seconds=10, poll_interval=0.01)
        self.assertEqual(status, "FINISHED")

    def test_wait_for_job_failure(self):
        client = StashClient(base_url="http://localhost:9999")
        statuses = [
            {"id": "1", "status": "RUNNING"},
            {"id": "1", "status": "FAILED", "error": "boom"},
        ]
        with patch.object(client, "get_job", side_effect=statuses):
            status = client.wait_for_job("1", timeout_seconds=10, poll_interval=0.01)
        self.assertEqual(status, "FAILED")

    def test_wait_for_job_timeout(self):
        client = StashClient(base_url="http://localhost:9999")
        with patch.object(client, "get_job", return_value={"id": "1", "status": "RUNNING"}):
            status = client.wait_for_job("1", timeout_seconds=0.01, poll_interval=0.05)
        self.assertEqual(status, "TIMEOUT")


class TestDownloadFile(unittest.TestCase):
    def test_relative_download_link_joins_base_url(self):
        client = StashClient(base_url="http://stash:9999", api_key="KEY")
        req_holder = {}

        def fake_urlopen(req, timeout=None):
            req_holder["req"] = req
            return FakeResponse(b"file-bytes")

        dest = tempfile.mktemp()
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            client.download_file("/downloads/abcd/export.zip", dest)

        self.assertEqual(req_holder["req"].full_url, "http://stash:9999/downloads/abcd/export.zip")
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), b"file-bytes")
        os.unlink(dest)

    def test_absolute_download_link_used_verbatim(self):
        client = StashClient(base_url="http://stash:9999")
        req_holder = {}

        def fake_urlopen(req, timeout=None):
            req_holder["req"] = req
            return FakeResponse(b"file-bytes")

        dest = tempfile.mktemp()
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            client.download_file("http://stash:9999/downloads/abcd/x.zip", dest)
        self.assertEqual(req_holder["req"].full_url, "http://stash:9999/downloads/abcd/x.zip")
        os.unlink(dest)


if __name__ == "__main__":
    unittest.main()
