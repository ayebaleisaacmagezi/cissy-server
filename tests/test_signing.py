"""Tests for release signing.

The Gradle template is the real Flutter 3.35 output, byte for byte, so a change
in a future Flutter version shows up here as a failing test rather than as a
silently debug-signed artifact.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from cissy import signing
from cissy.errors import CissyError, ValidationError
from cissy.signing import SigningCredentials

FLUTTER_TEMPLATE = """\
plugins {
    id("com.android.application")
    id("kotlin-android")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
}

android {
    namespace = "com.cissytech.portal"
    compileSdk = flutter.compileSdkVersion
    ndkVersion = flutter.ndkVersion

    defaultConfig {
        applicationId = "com.cissytech.portal"
        minSdk = flutter.minSdkVersion
        targetSdk = flutter.targetSdkVersion
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }

    buildTypes {
        release {
            // TODO: Add your own signing config for the release build.
            // Signing with the debug keys for now, so `flutter run --release` works.
            signingConfig = signingConfigs.getByName("debug")
        }
    }
}

flutter {
    source = "../.."
}
"""

# A plausible future template: the debug line is gone, the release block is not.
FUTURE_TEMPLATE = """\
plugins {
    id("com.android.application")
    id("dev.flutter.flutter-gradle-plugin")
}

android {
    namespace = "com.cissytech.portal"

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }
}
"""


class PatchTest(unittest.TestCase):
    def test_release_stops_using_the_debug_key(self):
        patched = signing.patch_gradle(FLUTTER_TEMPLATE)
        self.assertIn('signingConfig = signingConfigs.getByName("release")', patched)
        self.assertNotIn('getByName("debug")', patched)
        self.assertIn('create("release")', patched)

    def test_strips_comments_that_would_now_be_lies(self):
        patched = signing.patch_gradle(FLUTTER_TEMPLATE)
        self.assertNotIn("Add your own signing config", patched)
        self.assertNotIn("Signing with the debug keys", patched)

    def test_emits_an_order_gradle_accepts(self):
        # Imports must precede everything and `plugins {}` must be the first
        # statement, so the property loading has to sit between the two.
        patched = signing.patch_gradle(FLUTTER_TEMPLATE)
        self.assertTrue(patched.startswith("import java.io.FileInputStream"))

        positions = [
            patched.index("import java.util.Properties"),
            patched.index("plugins {"),
            patched.index("val cissyKeystoreProperties"),
            patched.index("\nandroid {"),
            patched.index("signingConfigs {"),
        ]
        self.assertEqual(positions, sorted(positions), patched)

    def test_running_twice_changes_nothing(self):
        # Scaffolds are reused between builds, so this runs on already-patched
        # files constantly.
        once = signing.patch_gradle(FLUTTER_TEMPLATE)
        self.assertEqual(signing.patch_gradle(once), once)
        self.assertEqual(once.count("signingConfigs {"), 1)
        self.assertEqual(once.count("import java.util.Properties"), 1)

    def test_handles_a_template_without_the_debug_line(self):
        patched = signing.patch_gradle(FUTURE_TEMPLATE)
        self.assertIn('signingConfig = signingConfigs.getByName("release")', patched)
        release_at = patched.index("release {")
        self.assertGreater(patched.index("getByName(\"release\")"), release_at)

    def test_refuses_rather_than_produce_an_unsigned_release(self):
        # Quietly shipping a debug-signed artifact that looks like a release is
        # the worst available outcome, so an unrecognisable file is an error.
        with self.assertRaises(CissyError):
            signing.patch_gradle("plugins {\n}\nandroid {\n}\n")

    def test_requires_a_plugins_block(self):
        with self.assertRaises(CissyError):
            signing.patch_gradle("android {\n    release {\n    }\n}\n")


class PropertiesTest(unittest.TestCase):
    def make(self, path=r"C:\keys\upload.jks") -> SigningCredentials:
        return SigningCredentials(
            keystore_path=Path(path),
            key_alias="upload",
            store_password="s3cret-store",
            key_password="s3cret-key",
        )

    def test_writes_what_gradle_reads(self):
        contents = self.make().properties_contents
        self.assertIn("keyAlias=upload", contents)
        self.assertIn("storePassword=s3cret-store", contents)
        self.assertIn("keyPassword=s3cret-key", contents)

    def test_paths_use_forward_slashes(self):
        # Backslashes are escape characters in a Java properties file, so a
        # Windows path written literally silently resolves to nothing.
        contents = self.make().properties_contents
        self.assertIn("storeFile=C:/keys/upload.jks", contents)
        self.assertNotIn("\\", contents)


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cissy_sign_"))
        gradle = self.root / "android" / "app" / "build.gradle.kts"
        gradle.parent.mkdir(parents=True)
        gradle.write_text(FLUTTER_TEMPLATE, encoding="utf-8")

        self.keystore = self.root / "upload.jks"
        self.keystore.write_bytes(b"not-a-real-keystore")
        self.credentials = SigningCredentials(
            keystore_path=self.keystore,
            key_alias="upload",
            store_password="pw",
            key_password="pw",
        )

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def key_properties(self) -> Path:
        return self.root / "android" / "key.properties"

    def test_writes_credentials_and_patches_gradle(self):
        signing.apply(self.root, self.credentials)
        self.assertTrue(self.key_properties().is_file())
        gradle = (self.root / "android" / "app" / "build.gradle.kts").read_text()
        self.assertIn('create("release")', gradle)

    def test_cleanup_removes_the_passwords(self):
        signing.apply(self.root, self.credentials)
        signing.cleanup(self.root)
        self.assertFalse(self.key_properties().exists())

    def test_cleanup_is_safe_to_repeat(self):
        signing.cleanup(self.root)
        signing.cleanup(self.root)

    def test_building_unsigned_clears_stale_credentials(self):
        # A leftover file would silently sign a later build with old details,
        # which surfaces only as a Play rejection for the wrong signature.
        signing.apply(self.root, self.credentials)
        signing.apply(self.root, None)
        self.assertFalse(self.key_properties().exists())

    def test_a_missing_keystore_is_reported_before_the_build(self):
        self.keystore.unlink()
        with self.assertRaises(ValidationError) as caught:
            signing.apply(self.root, self.credentials)
        self.assertIn("Upload it again", str(caught.exception))

    def test_an_empty_password_is_refused(self):
        credentials = SigningCredentials(
            keystore_path=self.keystore,
            key_alias="upload",
            store_password="",
            key_password="pw",
        )
        with self.assertRaises(ValidationError):
            signing.apply(self.root, credentials)


if __name__ == "__main__":
    unittest.main()
