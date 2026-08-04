"""Tests for build bookkeeping and failure classification.

No real builds here — those need Gradle and several minutes. What is tested is
everything around them: the one-at-a-time rule, the log fan-out, and the
classifier that turns a wall of Gradle output into a sentence.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from cissy.build import Build, BuildRunner, classify
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
        # 4 GB cannot hold two Gradle builds, and a queue is more machinery than
        # a single-user server needs.
        running = Build(
            number=1,
            app_id=self.config.id,
            output="aab",
            version_name="1.0.0",
            version_code=1,
            signed=False,
        )
        self.runner._current = running

        with self.assertRaises(ConflictError) as caught:
            self.runner.start(self.config, output="aab", credentials=None)
        self.assertIn("already running", str(caught.exception))

    def test_build_numbers_do_not_repeat(self):
        for number in (1, 2, 3):
            self.store.build_dir(self.config.id, number).mkdir(parents=True)
        self.assertEqual(self.store.next_build_number(self.config.id), 4)


if __name__ == "__main__":
    unittest.main()
