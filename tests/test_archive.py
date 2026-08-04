"""Tests for the downloadable project archive.

The exclusions carry real weight: two of them keep the zip from being ~300 MB,
and two keep signing material out of a file that lands in a Downloads folder and
gets forwarded around.
"""

import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from cissy import archive


class ShouldIncludeTest(unittest.TestCase):
    def test_keeps_the_source(self):
        for kept in (
            "lib/main.dart",
            "pubspec.yaml",
            "android/app/src/main/AndroidManifest.xml",
            "ios/Runner/Info.plist",
        ):
            self.assertTrue(archive.should_include(Path(kept)), kept)

    def test_drops_rebuildable_output(self):
        for dropped in (
            "build/app/outputs/apk/release/app-release.apk",
            ".dart_tool/package_config.json",
            "android/.gradle/8.0/checksums.lock",
        ):
            self.assertFalse(archive.should_include(Path(dropped)), dropped)

    def test_drops_signing_material_wherever_it_sits(self):
        # key.properties holds both passwords in plain text; the keystore is the
        # key itself. Neither can be undone once it has left the server.
        for secret in (
            "android/key.properties",
            "key.properties",
            "android/app/upload.jks",
            "somewhere/deep/release.keystore",
            "certs/dist.p12",
        ):
            self.assertFalse(archive.should_include(Path(secret)), secret)


class WriteArchiveTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cissy_zip_"))
        self.project = self.root / "generated"
        self._write("lib/main.dart", "void main() {}")
        self._write("pubspec.yaml", "name: portal")
        self._write("android/key.properties", "storePassword=hunter2")
        self._write("android/app/upload.jks", "binary")
        self._write("build/app/outputs/app.apk", "x" * 1000)
        self._write(".dart_tool/version", "3.35")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, relative: str, contents: str) -> None:
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    def names(self, target: Path) -> set[str]:
        with zipfile.ZipFile(target) as handle:
            return set(handle.namelist())

    def test_nests_everything_under_one_folder(self):
        # Otherwise unzipping scatters a Flutter project across the current
        # directory.
        target = archive.write_archive(
            self.project, self.root / "out.zip", root_name="portal"
        )
        for name in self.names(target):
            self.assertTrue(name.startswith("portal/"), name)

    def test_contains_the_source(self):
        target = archive.write_archive(
            self.project, self.root / "out.zip", root_name="portal"
        )
        names = self.names(target)
        self.assertIn("portal/lib/main.dart", names)
        self.assertIn("portal/pubspec.yaml", names)

    def test_contains_no_secrets(self):
        target = archive.write_archive(
            self.project, self.root / "out.zip", root_name="portal"
        )
        blob = target.read_bytes()
        self.assertNotIn(b"hunter2", blob)
        for name in self.names(target):
            self.assertNotIn("key.properties", name)
            self.assertFalse(name.endswith(".jks"), name)

    def test_leaves_no_temp_file_behind(self):
        archive.write_archive(self.project, self.root / "out.zip", root_name="portal")
        self.assertEqual(list(self.root.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
