"""Reading the Firebase configuration files a customer uploads.

The case that matters is the mismatch: a file from the wrong Firebase app
produces a project that builds, signs, installs and silently never receives a
notification, so it has to be caught by reading the file rather than by anyone
noticing later.
"""

import json
import plistlib
import unittest

from cissy import firebase
from cissy.errors import ValidationError


def android_file(*packages: str, project: str = "jokeb-mobile") -> bytes:
    return json.dumps({
        "project_info": {"project_number": "123", "project_id": project},
        "client": [
            {
                "client_info": {
                    "mobilesdk_app_id": f"1:123:android:{index}",
                    "android_client_info": {"package_name": name},
                },
                "api_key": [{"current_key": "AIzaSy-not-a-real-key"}],
            }
            for index, name in enumerate(packages)
        ],
        "configuration_version": "1",
    }).encode("utf-8")


def ios_file(bundle: str = "com.jokeb.mobile", project: str = "jokeb-mobile") -> bytes:
    return plistlib.dumps({
        "PROJECT_ID": project,
        "BUNDLE_ID": bundle,
        "GOOGLE_APP_ID": "1:123:ios:abc",
        "API_KEY": "AIzaSy-not-a-real-key",
        "GCM_SENDER_ID": "123",
    })


class AndroidTest(unittest.TestCase):
    def test_reads_the_project_and_package(self):
        app = firebase.read(android_file("com.jokeb.mobile"), firebase.ANDROID)
        self.assertEqual(app.project_id, "jokeb-mobile")
        self.assertEqual(app.identifier, "com.jokeb.mobile")
        self.assertEqual(app.app_id, "1:123:android:0")

    def test_carries_every_registered_package(self):
        # Firebase writes every Android app in the project into one file, and
        # the Gradle plugin picks the one matching the applicationId.
        app = firebase.read(
            android_file("com.jokeb.mobile", "com.jokeb.staging"), firebase.ANDROID
        )
        self.assertEqual(app.identifiers, ("com.jokeb.mobile", "com.jokeb.staging"))
        self.assertTrue(firebase.matches(app, "com.jokeb.staging"))

    def test_rejects_something_that_is_not_json(self):
        with self.assertRaises(ValidationError) as caught:
            firebase.read(b"<html>404</html>", firebase.ANDROID)
        self.assertIn("google-services.json", str(caught.exception))

    def test_rejects_json_that_is_not_a_firebase_file(self):
        with self.assertRaises(ValidationError) as caught:
            firebase.read(b'{"hello": "world"}', firebase.ANDROID)
        self.assertIn("no Firebase project", str(caught.exception))

    def test_rejects_a_project_with_no_android_app(self):
        data = json.dumps({
            "project_info": {"project_id": "jokeb-mobile"}, "client": [],
        }).encode()
        with self.assertRaises(ValidationError) as caught:
            firebase.read(data, firebase.ANDROID)
        self.assertIn("add an Android app", str(caught.exception))


class IosTest(unittest.TestCase):
    def test_reads_the_project_and_bundle(self):
        app = firebase.read(ios_file(), firebase.IOS)
        self.assertEqual(app.project_id, "jokeb-mobile")
        self.assertEqual(app.identifier, "com.jokeb.mobile")

    def test_bundle_ids_compare_without_case(self):
        # Apple treats them that way, and the config schema does not force one.
        app = firebase.read(ios_file(bundle="com.Jokeb.Mobile"), firebase.IOS)
        self.assertTrue(firebase.matches(app, "com.jokeb.mobile"))

    def test_rejects_something_that_is_not_a_plist(self):
        with self.assertRaises(ValidationError) as caught:
            firebase.read(b"not a plist", firebase.IOS)
        self.assertIn("GoogleService-Info.plist", str(caught.exception))

    def test_rejects_a_plist_missing_its_ids(self):
        with self.assertRaises(ValidationError) as caught:
            firebase.read(plistlib.dumps({"API_KEY": "x"}), firebase.IOS)
        self.assertIn("missing", str(caught.exception))


class CheckTest(unittest.TestCase):
    def test_a_matching_file_passes(self):
        app = firebase.read(android_file("com.jokeb.mobile"), firebase.ANDROID)
        firebase.check(app, "com.jokeb.mobile")

    def test_a_mismatch_names_both_sides(self):
        # The customer is about to go and fix this in another tab, so the
        # message has to carry the value they need to paste.
        app = firebase.read(android_file("com.other.app"), firebase.ANDROID)
        with self.assertRaises(ValidationError) as caught:
            firebase.check(app, "com.jokeb.mobile")
        message = str(caught.exception)
        self.assertIn("com.other.app", message)
        self.assertIn("com.jokeb.mobile", message)
        self.assertIn("google-services.json", message)

    def test_a_mismatch_lists_every_package_in_the_file(self):
        app = firebase.read(
            android_file("com.a.one", "com.a.two"), firebase.ANDROID
        )
        with self.assertRaises(ValidationError) as caught:
            firebase.check(app, "com.jokeb.mobile")
        self.assertIn("com.a.one, com.a.two", str(caught.exception))

    def test_an_ios_mismatch_says_bundle_id(self):
        app = firebase.read(ios_file(bundle="com.other.app"), firebase.IOS)
        with self.assertRaises(ValidationError) as caught:
            firebase.check(app, "com.jokeb.mobile")
        self.assertIn("bundle ID", str(caught.exception))
