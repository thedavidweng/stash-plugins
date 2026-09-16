import os
import shutil
import sys
import tarfile
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from td_resolver import resolve_td_path

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def exec_capable_dir() -> str:
    """A temp dir on a filesystem that allows exec.

    Some hosts (Synology DSM) mount /tmp noexec, so os.access(X_OK) is
    False there regardless of the mode bits; the resolver legitimately
    refuses such binaries. Fall back to the plugin directory, which is the
    same filesystem the real td binary runs from.
    """
    for base in (None, PLUGIN_DIR):
        candidate = tempfile.mkdtemp(dir=base, prefix="td-resolver-test-")
        probe = os.path.join(candidate, "probe")
        with open(probe, "wb") as handle:
            handle.write(b"")
        os.chmod(probe, 0o755)
        if os.access(probe, os.X_OK):
            return candidate
        shutil.rmtree(candidate, ignore_errors=True)
    raise unittest.SkipTest("no exec-capable temp directory available")


class TestTDResolver(unittest.TestCase):
    def setUp(self):
        self.temp_dir = exec_capable_dir()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_resolves_explicit_executable(self):
        binary = os.path.join(self.temp_dir, "td")
        with open(binary, "wb") as handle:
            handle.write(b"binary")
        os.chmod(binary, 0o755)

        result = resolve_td_path(binary, self.temp_dir)

        self.assertEqual(result.path, binary)
        self.assertEqual(result.source, "configured binary")

    def test_extracts_configured_tarball_into_runtime_cache(self):
        archive = os.path.join(self.temp_dir, "td_linux_x86_64.tar.gz")
        with tempfile.TemporaryDirectory() as source_dir:
            binary = os.path.join(source_dir, "td")
            with open(binary, "wb") as handle:
                handle.write(b"linux binary")
            with tarfile.open(archive, "w:gz") as bundle:
                bundle.add(binary, arcname="td")

        result = resolve_td_path(archive, self.temp_dir)

        self.assertTrue(os.path.isfile(result.path))
        self.assertTrue(os.access(result.path, os.X_OK))
        with open(result.path, "rb") as handle:
            self.assertEqual(handle.read(), b"linux binary")
        self.assertEqual(result.source, "configured release archive")

    def test_extracts_configured_zip_without_directory_traversal(self):
        archive = os.path.join(self.temp_dir, "td_windows_x86_64.zip")
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("nested/td.exe", b"windows binary")

        result = resolve_td_path(archive, self.temp_dir)

        self.assertTrue(os.path.isfile(result.path))
        self.assertTrue(result.path.endswith("td.exe"))
        with open(result.path, "rb") as handle:
            self.assertEqual(handle.read(), b"windows binary")
        self.assertEqual(result.source, "configured release archive")

    def test_default_prefers_plugin_bin_binary(self):
        binary = os.path.join(self.temp_dir, "bin", "td")
        os.makedirs(os.path.dirname(binary))
        with open(binary, "wb") as handle:
            handle.write(b"bundled")
        os.chmod(binary, 0o755)

        result = resolve_td_path("", self.temp_dir)

        self.assertEqual(result.path, binary)
        self.assertEqual(result.source, "plugin directory")

    def test_rejects_missing_configured_path(self):
        missing = os.path.join(self.temp_dir, "missing.tgz")

        with self.assertRaises(ValueError):
            resolve_td_path(missing, self.temp_dir)


if __name__ == "__main__":
    unittest.main()
