import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from td_updater import install_td_core


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def tarball_bytes():
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as bundle:
        content = b"td binary"
        info = tarfile.TarInfo("td")
        info.size = len(content)
        bundle.addfile(info, io.BytesIO(content))
    return output.getvalue()


class TestTDUpdater(unittest.TestCase):
    def test_downloads_verifies_and_installs_matching_release(self):
        archive = tarball_bytes()
        digest = hashlib.sha256(archive).hexdigest()
        checksums = f"{digest}  td_linux_x86_64.tar.gz\n".encode()
        responses = {
            "https://api.github.com/repos/thedavidweng/tg-drive-cli/releases/latest": (
                b'{"tag_name":"v1.2.3"}'
            ),
            "https://github.com/thedavidweng/tg-drive-cli/releases/download/"
            "v1.2.3/checksums.txt": checksums,
            "https://github.com/thedavidweng/tg-drive-cli/releases/download/"
            "v1.2.3/td_linux_x86_64.tar.gz": archive,
        }

        def open_url(request, **kwargs):
            url = request.full_url if hasattr(request, "full_url") else request
            return FakeResponse(responses[url])

        with tempfile.TemporaryDirectory() as plugin_dir:
            with patch("td_updater.platform.system", return_value="Linux"), patch(
                "td_updater.platform.machine", return_value="x86_64"
            ), patch("td_updater.urlopen", side_effect=open_url), patch(
                "td_updater.subprocess.run",
                return_value=MagicMock(
                    returncode=0,
                    stdout=json.dumps({"ok": True, "data": {"version": "1.2.3"}}),
                    stderr="",
                ),
            ):
                result = install_td_core(plugin_dir, version="latest")

            installed = os.path.join(plugin_dir, "bin", "td")
            self.assertEqual(result["version"], "v1.2.3")
            self.assertEqual(result["asset"], "td_linux_x86_64.tar.gz")
            self.assertEqual(result["path"], installed)
            self.assertTrue(os.path.isfile(installed))
            with open(installed, "rb") as handle:
                self.assertEqual(handle.read(), b"td binary")

    def test_rejects_checksum_mismatch(self):
        archive = tarball_bytes()
        responses = {
            "https://github.com/thedavidweng/tg-drive-cli/releases/download/"
            "v1.2.3/checksums.txt": b"bad  td_linux_x86_64.tar.gz\n",
            "https://github.com/thedavidweng/tg-drive-cli/releases/download/"
            "v1.2.3/td_linux_x86_64.tar.gz": archive,
        }

        def open_url(request, **kwargs):
            url = request.full_url if hasattr(request, "full_url") else request
            return FakeResponse(responses[url])

        with tempfile.TemporaryDirectory() as plugin_dir:
            with patch("td_updater.platform.system", return_value="Linux"), patch(
                "td_updater.platform.machine", return_value="x86_64"
            ), patch("td_updater.urlopen", side_effect=open_url):
                with self.assertRaises(ValueError):
                    install_td_core(plugin_dir, version="v1.2.3")


if __name__ == "__main__":
    unittest.main()
