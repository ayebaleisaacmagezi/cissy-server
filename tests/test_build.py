"""Tests for build bookkeeping and failure classification.

No real builds here - those need Gradle and several minutes. What is tested is
everything around them: the one-at-a-time rule, the log fan-out, and the
classifier that turns a wall of Gradle output into a sentence.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from cissy.build import Artifact, Build, BuildRunner, _abi_order, _artifact_name, classify
from cissy.config import AppConfig
from cissy.errors import ConflictError
from cissy.store import ProjectStore


class ClassifyTest(unittest.TestCase):
    def test_recognises_gradle_running_out_of_memory(self):
        log = "> Task :app:compileReleaseKotlin\nExpiring Daemon because JVM heap space is exhausted"
        self.assertIn("out of memory", classify(log))

    def test_recognises_a_full_disk(self):
        self.assertIn("disk is full", classify("java.io.IOException: No space left on device"))

    def test_recognises_unaccepted_licences(self):
        hint = classify("You have not accepted the license agreements of the following SDK components")
        self.assertIn("sdkmanager --licenses", hint)

    def test_recognises_a_wrong_keystore_password(self):
        hint = classify("Failed to read key upload from store: keystore password was incorrect")
        self.assertIn("password", hint.lower())

    def test_recognises_a_missing_sdk(self):
        hint = classify("SDK location not found. Define a valid SDK location")
        self.assertIn("ANDROID_HOME", hint)

    def test_recognises_a_network_failure(self):
        hint = classify("Could not resolve com.android.tools.build:gradle:8.1.0")
        self.assertIn("network", hint)

    def test_recognises_a_plugin_too_old_for_the_gradle_plugin(self):
        # The raw log names a file inside pub-cache at line 44, which reads as
        # a problem with the user's app. It is not: it is the toolchain.
        hint = classify(
            "* What went wrong:\n"
            "A problem occurred evaluating project ':flutter_inappwebview_android'.\n"
            "> `getDefaultProguardFile('proguard-android.txt')` is no longer supported"
        )
        self.assertIn("AGP 9", hint)
        self.assertIn("Nothing in your app configuration", hint)

    def test_a_plugin_evaluation_failure_is_named_as_a_mismatch(self):
        hint = classify("A problem occurred evaluating project ':flutter_something'.")
        self.assertIn("toolchain mismatch", hint)

    def test_says_nothing_when_it_does_not_know(self):
        # A confident wrong explanation on top of a real log is worse than none.
        self.assertIsNone(classify("> Task :app:assembleRelease\nSomething odd happened"))

    def test_the_most_specific_rule_wins(self):
        # Gradle prints "Could not resolve" while dying of memory exhaustion; the
        # memory message is the actionable one.
        log = "Could not resolve all files\nExpiring Daemon because JVM heap space is exhausted"
        self.assertIn("out of memory", classify(log))


class BuildLogTest(unittest.TestCase):
    def make(self) -> Build:
        return Build(
            number=1,
            app_id="portal",
            owner="tester",
            output="aab",
            version_name="1.0.0",
            version_code=1,
            signed=False,
        )

    def test_a_subscriber_receives_the_backlog_without_a_gap(self):
        # A browser attaching mid-build must not miss the lines already emitted,
        # nor see one twice.
        build = self.make()
        build.log("first")
        build.log("second")

        received: list[str] = []
        backlog = build.subscribe(received.append)
        build.log("third")

        self.assertEqual(backlog, ["first", "second"])
        self.assertEqual(received, ["third"])

    def test_a_failing_subscriber_does_not_stop_the_build(self):
        # A browser that navigated away must not take the build with it.
        build = self.make()

        def broken(_: str) -> None:
            raise ConnectionResetError("gone")

        good: list[str] = []
        build.subscribe(broken)
        build.subscribe(good.append)
        build.log("still running")

        self.assertEqual(good, ["still running"])
        self.assertEqual(build.lines, ["still running"])

    def test_unsubscribe_stops_delivery(self):
        build = self.make()
        received: list[str] = []
        build.subscribe(received.append)
        build.unsubscribe(received.append)
        build.log("after")
        self.assertEqual(received, [])

    def test_json_reports_what_the_ui_needs(self):
        build = self.make()
        payload = build.to_json()
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["signed"], False)
        self.assertIn("duration", payload)


class OneAtATimeTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cissy_build_"))
        self.store = ProjectStore(self.root)
        self.runner = BuildRunner(self.store)
        self.config = self.store.create(
            AppConfig(
                id="",
                name="Portal",
                website_url="https://portal.cissytech.com",
                android_package_id="com.cissytech.portal",
                ios_bundle_id="com.cissytech.portal",
            )
        )

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_a_second_build_is_refused_with_an_explanation(self):
        # 4 GB cannot hold two Gradle builds.
        running = Build(
            number=1,
            app_id=self.config.id,
            owner="someone-else",
            output="aab",
            version_name="1.0.0",
            version_code=1,
            signed=False,
        )
        self.runner._current = running

        with self.assertRaises(ConflictError) as caught:
            self.runner.start(self.config, output="aab", credentials=None)
        message = str(caught.exception)
        self.assertIn("busy", message)
        # And it must not say whose. On a server with more than one customer
        # that sentence hands the other person's app name to whoever pressed
        # Build second.
        self.assertNotIn(self.config.id, message)
        self.assertNotIn("someone-else", message)

    def test_build_numbers_do_not_repeat(self):
        for number in (1, 2, 3):
            self.store.build_dir(self.config.id, number).mkdir(parents=True)
        self.assertEqual(self.store.next_build_number(self.config.id), 4)



class VersionCodeTest(unittest.TestCase):
    """The rule for picking a build's version code.

    Getting this wrong is expensive in a way that is invisible until upload:
    Play rejects a reused code after both the build and the upload have been
    waited through.
    """

    def make(self, version_code: int = 1) -> AppConfig:
        return AppConfig(
            id="portal",
            name="Portal",
            website_url="https://portal.cissytech.com",
            android_package_id="com.cissytech.portal",
            ios_bundle_id="com.cissytech.portal",
            version_code=version_code,
        )

    def test_a_first_build_keeps_the_code_it_was_created_with(self):
        # Bumping unconditionally shipped a brand-new app's first build as
        # version code 2 and silently burned 1, which reads as a bug to anyone
        # looking at their first artifact.
        self.assertEqual(self.make(1).next_version_code(used=set()), 1)

    def test_building_again_moves_past_the_used_code(self):
        self.assertEqual(self.make(1).next_version_code(used={1}), 2)

    def test_a_hand_set_code_is_left_alone_when_unused(self):
        # Someone migrating an existing Play listing sets 47 because that is
        # where their published app is. The first build here must be 47.
        self.assertEqual(self.make(47).next_version_code(used={1, 2}), 47)

    def test_it_clears_every_used_code_not_just_the_current_one(self):
        # Play rejects a code used by any earlier upload, so dropping the
        # config back to an old number must not resurrect a collision.
        self.assertEqual(self.make(3).next_version_code(used={1, 2, 3, 50}), 51)


class UsedVersionCodesTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cissy_codes_"))
        self.store = ProjectStore(self.root)
        self.config = self.store.create(
            AppConfig(
                id="",
                name="Portal",
                website_url="https://portal.cissytech.com",
                android_package_id="com.cissytech.portal",
                ios_bundle_id="com.cissytech.portal",
            )
        )

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def record(self, number: int, body: str) -> None:
        directory = self.store.build_dir(self.config.id, number)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "build.json").write_text(body, encoding="utf-8")

    def test_reads_codes_from_the_build_records(self):
        self.record(1, '{"version_code": 1}')
        self.record(2, '{"version_code": 2}')
        self.assertEqual(self.store.used_version_codes(self.config.id), {1, 2})

    def test_no_builds_means_nothing_used(self):
        self.assertEqual(self.store.used_version_codes(self.config.id), set())

    def test_a_damaged_record_does_not_hide_the_others(self):
        # Losing one record to a truncated write must not make a used code look
        # free - that is the one outcome Play punishes.
        self.record(1, '{"version_code": 1}')
        self.record(2, "{ this is not json")
        self.record(3, '{"version_code": 3}')
        self.assertEqual(self.store.used_version_codes(self.config.id), {1, 3})


class BuildArgsTest(unittest.TestCase):
    def make(self, output: str) -> Build:
        return Build(
            number=1,
            app_id="portal",
            owner="tester",
            output=output,
            version_name="2.1.0",
            version_code=7,
            signed=False,
        )

    def config(self) -> AppConfig:
        return AppConfig(
            id="portal",
            name="Portal",
            website_url="https://portal.cissytech.com",
            android_package_id="com.cissytech.portal",
            ios_bundle_id="com.cissytech.portal",
            version_name="2.1.0",
            version_code=7,
        )

    def args(self, output: str) -> list[str]:
        runner = BuildRunner.__new__(BuildRunner)
        return runner._build_args(self.make(output), self.config())

    def test_apk_builds_are_split_by_architecture(self):
        # A universal APK is ~40 MB against ~15 MB split, and nearly all of the
        # difference is native code for architectures the phone will not use.
        self.assertIn("--split-per-abi", self.args("apk"))

    def test_bundles_are_not_split(self):
        # Play splits the bundle itself, and the flag is not valid here.
        self.assertNotIn("--split-per-abi", self.args("aab"))

    def test_the_version_reaches_the_build(self):
        args = self.args("aab")
        self.assertIn("--build-name=2.1.0", args)
        self.assertIn("--build-number=7", args)


class AbiOrderTest(unittest.TestCase):
    def test_arm64_comes_first(self):
        # It is the one to send someone who just wants to install the app;
        # installing the wrong one fails with a message that explains nothing.
        names = [
            Path("app-x86_64-release.apk"),
            Path("app-armeabi-v7a-release.apk"),
            Path("app-arm64-v8a-release.apk"),
        ]
        self.assertEqual(
            [p.name for p in sorted(names, key=_abi_order)],
            [
                "app-arm64-v8a-release.apk",
                "app-armeabi-v7a-release.apk",
                "app-x86_64-release.apk",
            ],
        )

    def test_an_unsplit_apk_still_sorts(self):
        self.assertEqual(_abi_order(Path("app-release.apk"))[0], 4)


class ArtifactNameTest(unittest.TestCase):
    """Downloads are named after the app and its version, not after Flutter's
    internal output files - `app-release.apk` tells the customer nothing."""

    def build(self, version="2.1.0"):
        return Build(
            number=1, app_id="shop", owner="u1", output="apk",
            version_name=version, version_code=7, signed=False,
        )

    def config(self, **overrides):
        base = dict(id="shop", name="My Shop", app_name="My Shop",
                    website_url="https://shop.example.com",
                    android_package_id="com.example.shop",
                    ios_bundle_id="com.example.shop")
        base.update(overrides)
        return AppConfig(**base)

    def test_split_apks_keep_their_abi(self):
        name = _artifact_name(
            self.config(), self.build(), Path("app-arm64-v8a-release.apk"), "apk")
        self.assertEqual(name, "MyShop-2.1.0-arm64-v8a.apk")

    def test_a_universal_apk_is_just_name_and_version(self):
        name = _artifact_name(
            self.config(), self.build(), Path("app-release.apk"), "apk")
        self.assertEqual(name, "MyShop-2.1.0.apk")

    def test_bundles_are_named_the_same_way(self):
        name = _artifact_name(
            self.config(), self.build(), Path("app-release.aab"), "aab")
        self.assertEqual(name, "MyShop-2.1.0.aab")

    def test_a_name_with_nothing_usable_falls_back_to_the_app_id(self):
        name = _artifact_name(
            self.config(app_name="!!!", name="!!!"), self.build(),
            Path("app-release.apk"), "apk")
        self.assertEqual(name, "shop-2.1.0.apk")


if __name__ == "__main__":
    unittest.main()

class ArtifactTokenTest(unittest.TestCase):
    """Every artifact mints its own opaque download token."""

    def test_the_token_is_random_and_in_the_json(self):
        one = Artifact(name="a.apk", path=Path("a.apk"), size=1, kind="apk")
        two = Artifact(name="b.apk", path=Path("b.apk"), size=1, kind="apk")
        self.assertGreaterEqual(len(one.token), 12)
        self.assertNotEqual(one.token, two.token)
        self.assertEqual(one.to_json()["token"], one.token)