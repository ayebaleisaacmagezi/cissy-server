"""Tests for the Android toolchain pin.

The failure this prevents is expensive to diagnose from the raw log: Gradle
reports a syntax problem at line 44 of a file inside pub-cache, which reads as a
broken dependency rather than a version mismatch, and it happens before any app
code compiles.
"""

import dataclasses
import shutil
import tempfile
import unittest
from pathlib import Path

from cissy import template
from cissy.config import AppConfig
from cissy.generate import PINNED_AGP, PINNED_GRADLE, _apply_launch_screen, _pin_android_toolchain
from cissy.store import ProjectStore

AGP_9_SETTINGS = """\
pluginManagement {
    repositories {
        google()
        mavenCentral()
    }
}

plugins {
    id("dev.flutter.flutter-plugin-loader") version "1.0.0"
    id("com.android.application") version "9.0.0" apply false
    id("org.jetbrains.kotlin.android") version "2.1.0" apply false
}
"""

AGP_8_SETTINGS = AGP_9_SETTINGS.replace('version "9.0.0"', 'version "8.9.1"')

WRAPPER_9 = (
    "distributionBase=GRADLE_USER_HOME\n"
    "distributionUrl=https\\://services.gradle.org/distributions/gradle-9.0-all.zip\n"
)


class PinTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cissy_pin_"))
        self.logs: list[str] = []
        (self.root / "android" / "gradle" / "wrapper").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    @property
    def settings(self) -> Path:
        return self.root / "android" / "settings.gradle.kts"

    @property
    def wrapper(self) -> Path:
        return self.root / "android" / "gradle" / "wrapper" / "gradle-wrapper.properties"

    def write(self, settings: str, wrapper: str | None = WRAPPER_9) -> None:
        self.settings.write_text(settings, encoding="utf-8")
        if wrapper is not None:
            self.wrapper.write_text(wrapper, encoding="utf-8")

    def pin(self) -> None:
        _pin_android_toolchain(self.root, self.logs.append)

    def test_agp_9_is_pinned_back(self):
        self.write(AGP_9_SETTINGS)
        self.pin()
        self.assertIn(f'version "{PINNED_AGP}"', self.settings.read_text())
        self.assertNotIn('version "9.0.0"', self.settings.read_text())

    def test_gradle_is_pinned_with_it(self):
        # AGP 8.9 does not run under Gradle 9, so pinning one alone trades this
        # failure for a less obvious one.
        self.write(AGP_9_SETTINGS)
        self.pin()
        self.assertIn(f"gradle-{PINNED_GRADLE}-all.zip", self.wrapper.read_text())

    def test_it_says_what_it_changed_and_why(self):
        self.write(AGP_9_SETTINGS)
        self.pin()
        joined = "\n".join(self.logs)
        self.assertIn("9.0.0", joined)
        self.assertIn(PINNED_AGP, joined)
        self.assertIn("AGP 9", joined)

    def test_an_agp_8_scaffold_is_left_alone(self):
        # Flutter 3.35.5 already scaffolds a working pair. Rewriting it would be
        # churn with nothing to gain and a version to keep chasing.
        self.write(AGP_8_SETTINGS, wrapper=None)
        self.pin()
        self.assertEqual(self.settings.read_text(), AGP_8_SETTINGS)
        self.assertEqual(self.logs, [])

    def test_only_the_kotlin_plugin_is_untouched(self):
        self.write(AGP_9_SETTINGS)
        self.pin()
        self.assertIn('id("org.jetbrains.kotlin.android") version "2.1.0"',
                      self.settings.read_text())

    def test_running_twice_changes_nothing_further(self):
        # Scaffolds are reused between builds, so this runs on already-pinned
        # projects constantly.
        self.write(AGP_9_SETTINGS)
        self.pin()
        once = self.settings.read_text()
        self.logs.clear()
        self.pin()
        self.assertEqual(self.settings.read_text(), once)
        self.assertEqual(self.logs, [])

    def test_an_unrecognisable_scaffold_is_reported_not_guessed_at(self):
        # Silently leaving it alone would surface as the original confusing
        # Gradle error with nothing connecting the two.
        self.write("plugins {\n    id(\"something.else\") version \"1.0\"\n}\n")
        self.pin()
        self.assertTrue(any("could not find" in line for line in self.logs))

    def test_a_missing_settings_file_is_not_an_error(self):
        _pin_android_toolchain(self.root, self.logs.append)
        self.assertEqual(self.logs, [])

    def test_a_missing_wrapper_does_not_undo_the_agp_pin(self):
        self.write(AGP_9_SETTINGS, wrapper=None)
        self.pin()
        self.assertIn(f'version "{PINNED_AGP}"', self.settings.read_text())


class LaunchScreenTest(unittest.TestCase):
    """The tap-to-app moment: splash straight away, no flash of the icon.

    The launch window must carry the splash image itself, and on Android 12+
    the system splash must be handed a transparent icon — including in dark
    mode, where `values-night` would otherwise outrank `values-v31` and bring
    the icon back.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cissy_launch_"))
        self.store = ProjectStore(self.root)
        self.config = AppConfig(
            id="shop",
            name="Shop",
            website_url="https://shop.example.com",
            android_package_id="com.example.shop",
            ios_bundle_id="com.example.shop",
            splash_file="splash.png",
        )
        self.project = self.root / "shop" / "generated"
        (self.project / "android").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    @property
    def res(self) -> Path:
        return self.project / "android" / "app" / "src" / "main" / "res"

    def upload_splash(self, name: str = "splash.png") -> None:
        assets = self.store.assets_dir("shop")
        assets.mkdir(parents=True, exist_ok=True)
        (assets / name).write_bytes(b"not-really-a-png")

    def apply(self, config: AppConfig | None = None) -> None:
        _apply_launch_screen(config or self.config, self.store, self.project)

    def test_the_launch_window_is_the_splash_image(self):
        self.upload_splash()
        self.apply()
        for variant in ("drawable", "drawable-v21"):
            contents = (self.res / variant / "launch_background.xml").read_text()
            self.assertIn(template.SPLASH_DRAWABLE, contents)
        self.assertTrue((self.res / "drawable" / "cissy_splash.png").is_file())

    def test_android_12_gets_a_transparent_icon_in_both_modes(self):
        self.upload_splash()
        self.apply()
        for variant in ("values-v31", "values-night-v31"):
            contents = (self.res / variant / "styles.xml").read_text()
            self.assertIn("windowSplashScreenAnimatedIcon", contents)
            self.assertIn(f"{template.SPLASH_DRAWABLE}_icon", contents)
        icon = (self.res / "drawable" / "cissy_splash_icon.xml").read_text()
        self.assertIn("transparent", icon)

    def test_no_splash_means_the_stock_launch_screen(self):
        self.apply()
        contents = (self.res / "drawable" / "launch_background.xml").read_text()
        self.assertNotIn(template.SPLASH_DRAWABLE, contents)
        self.assertFalse((self.res / "values-v31" / "styles.xml").exists())

    def test_removing_the_splash_reverts_everything(self):
        # The scaffold survives between builds, so a removed splash has to
        # take its overrides with it rather than leaving a stale launch screen.
        self.upload_splash()
        self.apply()
        without = dataclasses.replace(self.config, splash_file=None)
        self.apply(without)
        contents = (self.res / "drawable" / "launch_background.xml").read_text()
        self.assertNotIn(template.SPLASH_DRAWABLE, contents)
        self.assertFalse(list((self.res / "drawable").glob("cissy_splash.*")))
        self.assertFalse((self.res / "values-v31" / "styles.xml").exists())
        self.assertFalse((self.res / "values-night-v31" / "styles.xml").exists())

    def test_a_jpeg_upload_is_stored_as_jpg(self):
        # aapt does not accept a .jpeg resource file name.
        config = dataclasses.replace(self.config, splash_file="splash.jpeg")
        self.upload_splash("splash.jpeg")
        self.apply(config)
        self.assertTrue((self.res / "drawable" / "cissy_splash.jpg").is_file())
        self.assertFalse((self.res / "drawable" / "cissy_splash.jpeg").exists())

    def test_a_changed_extension_leaves_one_resource_not_two(self):
        # Two files sharing one resource name is a hard aapt error.
        self.upload_splash()
        self.apply()
        config = dataclasses.replace(self.config, splash_file="splash.jpg")
        self.upload_splash("splash.jpg")
        self.apply(config)
        found = sorted(p.name for p in (self.res / "drawable").glob("cissy_splash.*"))
        self.assertEqual(found, ["cissy_splash.jpg"])


if __name__ == "__main__":
    unittest.main()
