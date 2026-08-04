"""Tests for the generated app's source.

These assert on strings, which cannot prove the Dart compiles — that is done by
generating a real project and running `flutter analyze` on it, for both a
fully-featured and a bare configuration. What is locked in here is the reasoning
that is easy to break silently: which blocks appear for which features, and that
nothing references a block that was not emitted.
"""

import unittest

from cissy import template
from cissy.config import AppConfig

ALL_FEATURES = (
    "File upload",
    "Downloads",
    "Native sharing",
    "Pull to refresh",
    "Camera",
    "Location",
    "Deep links",
)


def make(**overrides) -> AppConfig:
    base = dict(
        id="portal",
        name="Cissytech Portal",
        website_url="https://portal.cissytech.com",
        android_package_id="com.cissytech.portal",
        ios_bundle_id="com.cissytech.portal",
        allowed_domains=("portal.cissytech.com",),
    )
    base.update(overrides)
    return AppConfig(**base)


class NamingTest(unittest.TestCase):
    def test_project_name_comes_from_the_package_id(self):
        # So that `flutter create --org/--project-name` yields exactly the
        # configured applicationId, leaving nothing to patch afterwards.
        config = make(android_package_id="com.cissytech.portal")
        self.assertEqual(template.project_name(config), "portal")
        self.assertEqual(template.organisation(config), "com.cissytech")

    def test_project_name_avoids_dart_keywords(self):
        self.assertEqual(template.project_name(make(android_package_id="com.x.test")), "test_app")

    def test_deep_link_scheme_never_starts_with_a_digit(self):
        # Android silently refuses to register such a scheme.
        scheme = template.deep_link_scheme(make(app_name="4 Corners"))
        self.assertTrue(scheme[0].isalpha(), scheme)
        self.assertEqual(scheme, "corners")

    def test_deep_link_scheme_falls_back_when_nothing_survives(self):
        self.assertEqual(template.deep_link_scheme(make(app_name="!!!")), "cissyapp")


class DartStringTest(unittest.TestCase):
    def test_escapes_quotes_and_backslashes(self):
        self.assertEqual(template.dart_string('a"b\\c'), '"a\\"b\\\\c"')

    def test_a_hostile_app_name_cannot_break_out_of_the_literal(self):
        # The name reaches Dart source, so an unescaped quote would be a
        # compile error at best and injected code at worst.
        source = template.main_dart(make(app_name='Evil"); exit(0); //'))
        self.assertIn('title: "Evil\\"); exit(0); //"', source)


class FeatureGatingTest(unittest.TestCase):
    def test_bare_config_omits_every_optional_block(self):
        source = template.main_dart(make(offline_fallback_enabled=False))
        for absent in (
            "_ErrorView",
            "_failFromError",
            "_failFromStatus",
            "offlineErrorTypes",
            "_handleBridge",
            "_download",
            "AppLinks",
            "PullToRefreshController(",
            "onPermissionRequest",
        ):
            self.assertNotIn(absent, source, absent)

    def test_every_feature_emits_its_block(self):
        source = template.main_dart(make(features=ALL_FEATURES))
        for present in (
            "_handleBridge",
            "SharePlus.instance.share",
            "Geolocator.getCurrentPosition",
            "_download(",
            "AppLinks()",
            "PullToRefreshController(",
            "onPermissionRequest",
        ):
            self.assertIn(present, source, present)

    def test_load_is_emitted_whenever_something_calls_it(self):
        # Dead code fails the generated project's own flutter_lints run, and a
        # missing definition fails the compile. Both directions matter.
        deep_only = template.main_dart(
            make(features=("Deep links",), offline_fallback_enabled=False)
        )
        self.assertIn("Future<void> _load(", deep_only)

        fallback_only = template.main_dart(make(offline_fallback_enabled=True))
        self.assertIn("Future<void> _load(", fallback_only)

        neither = template.main_dart(make(offline_fallback_enabled=False))
        self.assertNotIn("Future<void> _load(", neither)

    def test_allowed_web_uri_only_exists_for_deep_links(self):
        self.assertIn("_isAllowedWebUri", template.main_dart(make(features=("Deep links",))))
        self.assertNotIn("_isAllowedWebUri", template.main_dart(make()))

    def test_file_upload_off_injects_the_blocking_script(self):
        source = template.main_dart(make(features=()))
        self.assertIn('input[type="file"]', source)
        self.assertIn("MutationObserver", source)

    def test_file_upload_on_leaves_the_page_alone(self):
        source = template.main_dart(make(features=("File upload",)))
        self.assertNotIn('input[type="file"]', source)


class ErrorScreenTest(unittest.TestCase):
    def test_classifies_the_failures_a_user_can_act_on(self):
        source = template.main_dart(make())
        for phrase in (
            "You appear to be offline",
            "Page not found",
            "Access denied",
            "This is taking too long",
            "Try again",
            "Go to home page",
        ):
            self.assertIn(phrase, source, phrase)

    def test_matches_offline_errors_by_string_not_by_constant(self):
        # flutter_inappwebview's error types are `static final`, so a const set
        # of them would not compile. The string values are also stable across
        # package versions.
        source = template.main_dart(make())
        self.assertIn("error.type.toValue()", source)


class SettingsTest(unittest.TestCase):
    def test_carries_webview_settings_through(self):
        source = template.main_dart(
            make(
                javascript_enabled=False,
                dom_storage_enabled=False,
                cache_enabled=False,
                custom_user_agent="CissyBot/1.0",
            )
        )
        self.assertIn("javaScriptEnabled: false", source)
        self.assertIn("domStorageEnabled: false", source)
        self.assertIn("cacheMode: CacheMode.LOAD_NO_CACHE", source)
        self.assertIn('userAgent: "CissyBot/1.0"', source)

    def test_omits_the_user_agent_when_unset(self):
        self.assertIn("userAgent: null", template.main_dart(make()))

    def test_external_link_names_match_the_config_vocabulary(self):
        from cissy.config import EXTERNAL_LINK_BEHAVIOURS

        source = template.main_dart(make())
        for behaviour in ("webview", "browser"):
            self.assertIn(f"externalLinkBehavior == '{behaviour}'", source)
            self.assertIn(behaviour, EXTERNAL_LINK_BEHAVIOURS)


class PubspecTest(unittest.TestCase):
    def test_only_pulls_dependencies_the_features_need(self):
        bare = template.pubspec(make())
        self.assertNotIn("share_plus", bare)
        self.assertNotIn("geolocator", bare)
        self.assertNotIn("app_links", bare)

        full = template.pubspec(make(features=ALL_FEATURES))
        for package in ("share_plus", "geolocator", "app_links", "open_filex"):
            self.assertIn(package, full)

    def test_version_follows_the_config(self):
        self.assertIn(
            "version: 2.1.0+34", template.pubspec(make(version_name="2.1.0", version_code=34))
        )


class AndroidManifestTest(unittest.TestCase):
    def test_always_requests_internet(self):
        self.assertIn("android.permission.INTERNET", template.android_manifest(make()))

    def test_only_requests_permissions_for_enabled_features(self):
        bare = template.android_manifest(make())
        self.assertNotIn("permission.CAMERA", bare)
        self.assertNotIn("ACCESS_FINE_LOCATION", bare)

        full = template.android_manifest(make(features=("Camera", "Location")))
        self.assertIn("permission.CAMERA", full)
        self.assertIn("ACCESS_FINE_LOCATION", full)

    def test_escapes_the_app_label(self):
        # An ampersand in an app name would otherwise produce invalid XML and a
        # build failure that says nothing about the app name.
        manifest = template.android_manifest(make(app_name="Tools & Parts"))
        self.assertIn('android:label="Tools &amp; Parts"', manifest)

    def test_cleartext_traffic_follows_require_https(self):
        self.assertIn(
            'usesCleartextTraffic="false"', template.android_manifest(make())
        )
        self.assertIn(
            'usesCleartextTraffic="true"',
            template.android_manifest(make(require_https=False, website_url="http://x.lan")),
        )

    def test_deep_link_filters_appear_only_when_enabled(self):
        self.assertNotIn("autoVerify", template.android_manifest(make()))
        with_links = template.android_manifest(make(features=("Deep links",)))
        self.assertIn('android:host="portal.cissytech.com"', with_links)
        self.assertIn('android:scheme="cissytechportal"', with_links)

    def test_activity_matches_the_package(self):
        manifest = template.android_manifest(make())
        self.assertIn('android:name="com.cissytech.portal.MainActivity"', manifest)


class IosTest(unittest.TestCase):
    def test_no_permissions_means_no_usage_strings(self):
        self.assertEqual(template.ios_usage_descriptions(make()), {})

    def test_camera_brings_photos_with_it(self):
        keys = template.ios_usage_descriptions(make(features=("Camera",)))
        self.assertIn("NSCameraUsageDescription", keys)
        self.assertIn("NSPhotoLibraryUsageDescription", keys)

    def test_file_upload_alone_still_needs_photo_access(self):
        keys = template.ios_usage_descriptions(make(features=("File upload",)))
        self.assertEqual(list(keys), ["NSPhotoLibraryUsageDescription"])

    def test_defaults_name_the_app(self):
        # Vague reasons are a common App Store rejection.
        keys = template.ios_usage_descriptions(make(features=("Location",)))
        self.assertIn(
            "Cissytech Portal", keys["NSLocationWhenInUseUsageDescription"]
        )

    def test_a_custom_reason_wins(self):
        config = make(
            features=("Camera",),
            permission_descriptions={"camera": "To scan delivery barcodes."},
        )
        keys = template.ios_usage_descriptions(config)
        self.assertEqual(keys["NSCameraUsageDescription"], "To scan delivery barcodes.")


class GradlePropertiesTest(unittest.TestCase):
    def test_disables_the_daemon(self):
        # It holds well over a gigabyte between builds, which is wasted on a
        # server that builds a couple of times a week.
        self.assertIn("org.gradle.daemon=false", template.gradle_properties())


if __name__ == "__main__":
    unittest.main()


class LauncherIconTest(unittest.TestCase):
    """Icons were uploaded and stored but never reached the built app.

    Android reads the launcher icon from five mipmap densities and iOS from
    about fifteen sizes in an appiconset. Nothing produced those, so every
    generated app shipped with the default Flutter icon.
    """

    def test_no_icon_means_no_config_block(self):
        pubspec = template.pubspec(make(), None, None)
        self.assertNotIn("flutter_launcher_icons", pubspec)

    def test_an_icon_adds_the_tool_and_its_config(self):
        pubspec = template.pubspec(make(), None, "assets/icon/icon.png")
        self.assertIn("flutter_launcher_icons: ^0.14.4", pubspec)
        self.assertIn("\nflutter_launcher_icons:\n", pubspec)
        self.assertIn("image_path: assets/icon/icon.png", pubspec)

    def test_it_strips_alpha_for_ios(self):
        # iOS rejects an icon with an alpha channel, and a logo exported with a
        # transparent background is the normal case.
        self.assertIn("remove_alpha_ios: true", template.pubspec(make(), None, "a.png"))

    def test_the_tool_is_a_dev_dependency(self):
        # It runs at build time and must not ship inside the app.
        pubspec = template.pubspec(make(), None, "a.png")
        dev_block = pubspec.split("dev_dependencies:")[1].split("\nflutter:")[0]
        self.assertIn("flutter_launcher_icons", dev_block)

    def test_the_splash_asset_is_unaffected(self):
        pubspec = template.pubspec(make(), "assets/splash.png", "assets/icon/icon.png")
        self.assertIn("  assets:", pubspec)
        self.assertIn("    - assets/splash.png", pubspec)
        self.assertIn("image_path: assets/icon/icon.png", pubspec)
