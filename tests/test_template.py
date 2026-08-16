"""Tests for the generated app's source.

These assert on strings, which cannot prove the Dart compiles - that is done by
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
        # compile error at best and injected code at worst. The title now
        # lives in the appTitle constant rather than inline in MaterialApp.
        source = template.main_dart(make(app_name='Evil"); exit(0); //'))
        self.assertIn('appTitle = "Evil\\"); exit(0); //"', source)


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

class CustomOfflineScreenTest(unittest.TestCase):
    """The developer's own HTML replaces the built-in error view.

    The contract with that HTML: app://retry retries, app://home goes to the
    start page, and the theme colour arrives as the --accent CSS variable.
    """

    def test_the_custom_view_replaces_the_built_in_one(self):
        source = template.main_dart(make(), offline_asset="assets/offline.html")
        self.assertIn("_CustomErrorView", source)
        self.assertNotIn("class _ErrorView", source)
        self.assertIn('"assets/offline.html"', source)

    def test_the_links_are_the_controls(self):
        source = template.main_dart(make(), offline_asset="assets/offline.html")
        self.assertIn("uri.scheme == 'app'", source)
        self.assertIn("'retry'", source)
        self.assertIn("'home'", source)

    def test_without_custom_html_the_built_in_screen_remains(self):
        source = template.main_dart(make())
        self.assertIn("class _ErrorView", source)
        self.assertNotIn("_CustomErrorView", source)

    def test_the_theme_colour_is_injected_as_accent(self):
        source = template.main_dart(
            make(theme_color="#b3561d"), offline_asset="assets/offline.html"
        )
        self.assertIn("--accent", source)
        self.assertIn("#b3561d", source)

    def test_no_theme_colour_means_no_injection(self):
        source = template.main_dart(make(), offline_asset="assets/offline.html")
        self.assertNotIn("--accent", source)

    def test_fallback_off_beats_the_asset(self):
        # The generator never passes an asset while the module is off, but the
        # template must not depend on it remembering that.
        source = template.main_dart(
            make(offline_fallback_enabled=False), offline_asset="assets/offline.html"
        )
        self.assertNotIn("_CustomErrorView", source)

    def test_pubspec_bundles_the_asset(self):
        pubspec = template.pubspec(make(), None, None, "assets/offline.html")
        self.assertIn("  assets:", pubspec)
        self.assertIn("    - assets/offline.html", pubspec)

    def test_pubspec_keeps_the_splash_alongside(self):
        pubspec = template.pubspec(
            make(), "assets/splash.png", None, "assets/offline.html"
        )
        self.assertIn("    - assets/splash.png", pubspec)
        self.assertIn("    - assets/offline.html", pubspec)

class SplashTest(unittest.TestCase):
    """The icon on a background, which is what an app gets without uploading."""

    def dart(self, **extra):
        return template.main_dart(
            make(**extra), extra.pop("_splash", None), None, "assets/icon/icon.png"
        )

    def test_the_icon_is_centred_on_the_background(self):
        source = self.dart()
        self.assertIn("ColoredBox(", source)
        self.assertIn('"assets/icon/icon.png"', source)

    def test_the_background_follows_the_phone_into_dark_mode(self):
        source = self.dart()
        self.assertIn("MediaQuery.platformBrightnessOf(context)", source)
        self.assertIn("0xFFFFFFFF", source)
        self.assertIn("0xFF101014", source)

    def test_one_colour_for_both_modes_asks_no_question(self):
        # Same colour either way, so the brightness check would be dead code.
        source = self.dart(splash_bg_light="#222222", splash_bg_dark="#222222")
        self.assertIn("0xFF222222", source)
        self.assertNotIn("platformBrightnessOf", source)

    def test_the_image_style_fills_the_screen(self):
        source = template.main_dart(
            make(splash_style="image"), "assets/splash.png", None, "assets/icon/i.png"
        )
        self.assertIn('"assets/splash.png"', source)
        self.assertIn("BoxFit.cover", source)
        # The icon is the other style's business.
        self.assertNotIn('"assets/icon/i.png"', source)
        self.assertNotIn("platformBrightnessOf", source)

    def test_no_icon_still_paints_the_background(self):
        # Better a plain surface than an empty box: it reads as the app
        # starting rather than as a failure.
        source = template.main_dart(make(), None, None, None)
        self.assertIn("child: const SizedBox.expand(),", source)
        self.assertNotIn("Image.asset", source)

    def test_the_icon_is_bundled_only_when_it_is_drawn(self):
        with_icon = template.pubspec(make(), None, "assets/icon/i.png", None)
        self.assertIn("    - assets/icon/i.png", with_icon)
        as_image = template.pubspec(
            make(splash_style="image"), None, "assets/icon/i.png", None
        )
        self.assertNotIn("    - assets/icon/i.png", as_image)


class TopBarTest(unittest.TestCase):
    """The shell's top app bar earns its place or does not exist.

    With nothing to hold it would just be a strip of chrome between the user
    and their website, so it only appears when Native sharing puts a button
    in it.
    """

    NAV = dict(
        nav_style="bottom",
        nav_tabs=(
            {"label": "Home", "icon": "home", "target": "/"},
            {"label": "Shop", "icon": "storefront", "target": "/shop"},
        ),
    )

    def test_plain_navigation_has_no_top_bar(self):
        source = template.main_dart(make(**self.NAV))
        self.assertNotIn("Text(appTitle)", source)

    def test_a_module_button_brings_the_bar_with_it(self):
        source = template.main_dart(make(features=("Native sharing",), **self.NAV))
        self.assertIn("Text(appTitle)", source)
        self.assertIn("Icons.share_outlined", source)


class SitePolicyTest(unittest.TestCase):
    """Taking the website's own navigation down inside the app."""

    def dart(self, **overrides) -> str:
        return template.main_dart(make(**overrides))

    def test_nothing_is_emitted_when_nothing_is_configured(self):
        # An unused constant is dead code, which fails the generated
        # project's own lint run.
        source = self.dart()
        self.assertNotIn("sitePolicyScript", source)
        self.assertNotIn("dart:collection", source)

    def test_selectors_become_one_stylesheet(self):
        source = self.dart(hide_selectors=(".mobile-nav", "#footer .menu"))
        self.assertIn(
            '.mobile-nav, #footer .menu { display: none !important; }', source
        )
        self.assertIn("cissy-site-policy", source)

    def test_the_script_runs_at_document_start(self):
        # Waiting for the load to finish means the site's own bar paints and
        # then vanishes on every page.
        source = self.dart(hide_selectors=(".mobile-nav",))
        self.assertIn("UserScriptInjectionTime.AT_DOCUMENT_START", source)
        self.assertIn("import 'dart:collection';", source)

    def test_the_script_is_reapplied_when_a_load_finishes(self):
        # Single-page navigation replaces the DOM without a real load.
        source = self.dart(hide_selectors=(".mobile-nav",))
        self.assertIn("evaluateJavascript(source: sitePolicyScript)", source)

    def test_it_does_nothing_off_an_allowed_domain(self):
        # The app is not entitled to restyle somebody else's page.
        source = self.dart(hide_selectors=(".mobile-nav",))
        self.assertIn("if (!permitted) return;", source)
        self.assertIn("if (!_isAllowedHost(host))", source)

    def test_the_body_class_waits_for_a_body(self):
        # At document start there is no body yet.
        source = self.dart(body_class="web2app-native")
        self.assertIn('classList.add(marker)', source)
        self.assertIn("MutationObserver", source)

    def test_a_body_class_alone_emits_no_stylesheet(self):
        source = self.dart(body_class="web2app-native")
        self.assertIn("sitePolicyScript", source)
        self.assertNotIn("cissy-site-policy", source)

    def test_the_url_flag_reaches_the_home_url(self):
        source = self.dart(url_flag="source=web2app")
        self.assertIn(
            'const homeUrl = "https://portal.cissytech.com?source=web2app";',
            source,
        )

    def test_the_url_flag_joins_an_existing_query(self):
        source = template.main_dart(
            make(website_url="https://portal.cissytech.com/?a=b",
                 url_flag="source=web2app")
        )
        self.assertIn("?a=b&source=web2app", source)

    def test_the_url_flag_reaches_navigation_tabs(self):
        source = self.dart(
            url_flag="source=web2app",
            nav_style="bottom",
            nav_tabs=(
                {"label": "Home", "icon": "home", "target": "/"},
                {"label": "Shop", "icon": "storefront", "target": "/shop"},
            ),
        )
        self.assertIn('"https://portal.cissytech.com/shop?source=web2app"', source)

    def test_file_inputs_are_still_disabled_alongside_it(self):
        # Two policies in one method; neither may displace the other.
        source = self.dart(hide_selectors=(".mobile-nav",))
        self.assertIn('input[type="file"]', source)
        self.assertIn("sitePolicyScript", source)

    def test_uploads_leave_only_the_site_policy(self):
        source = self.dart(
            features=("File upload",), hide_selectors=(".mobile-nav",)
        )
        self.assertNotIn('input[type="file"]', source)
        self.assertIn("sitePolicyScript", source)


class TabEchoTest(unittest.TestCase):
    """Lighting up the tab a page belongs to, without switching to it."""

    def dart(self, tabs) -> str:
        return template.main_dart(make(nav_style="bottom", nav_tabs=tabs))

    PLAIN = (
        {"label": "Home", "icon": "home", "target": "/"},
        {"label": "Shop", "icon": "storefront", "target": "/shop"},
    )
    MATCHED = (
        {"label": "Home", "icon": "home", "target": "/"},
        {"label": "Account", "icon": "person", "target": "/account",
         "match": ["/profile", "/orders"]},
    )

    def test_a_tab_matches_its_own_path_without_being_told_to(self):
        source = self.dart(self.PLAIN)
        self.assertIn('navMatches = <List<String>>[["/"], ["/shop"]]', source)

    def test_configured_paths_follow_the_tab_s_own(self):
        source = self.dart(self.MATCHED)
        self.assertIn('["/account", "/profile", "/orders"]', source)

    def test_the_bar_prefers_the_echo(self):
        self.assertIn("selectedIndex: echoIndex ?? index", self.dart(self.PLAIN))

    def test_a_tap_clears_the_echo(self):
        # A tap is a decision; it outranks whatever the page was saying.
        source = self.dart(self.PLAIN)
        self.assertIn("echoIndex = null;", source)

    def test_only_the_visible_tab_reports(self):
        # Background tabs go on finishing loads of their own.
        source = self.dart(self.PLAIN)
        self.assertIn("if (from != index) {", source)

    def test_single_page_route_changes_are_heard(self):
        # pushState moves the address without a load.
        self.assertIn("onUpdateVisitedHistory:", self.dart(self.PLAIN))

    def test_nothing_is_emitted_without_navigation(self):
        source = template.main_dart(make())
        self.assertNotIn("navMatches", source)
        self.assertNotIn("echoIndex", source)

    def test_longest_match_wins(self):
        # Otherwise a tab on "/" outranks every other tab by matching first.
        self.assertIn("prefix.length > bestLength", self.dart(self.MATCHED))


class PushTest(unittest.TestCase):
    """The notification code in the generated app."""

    def dart(self, **overrides) -> str:
        base = dict(push_enabled=True, allowed_domains=("portal.cissytech.com",))
        base.update(overrides)
        return template.main_dart(make(**base))

    def test_nothing_is_emitted_when_push_is_off(self):
        source = template.main_dart(make())
        for name in ("PushService", "firebase_messaging", "pendingPushUrl"):
            self.assertNotIn(name, source)

    def test_firebase_is_up_before_the_first_frame(self):
        # The tap that started a terminated app is read during initialise, so
        # doing it after runApp loses it.
        source = self.dart()
        self.assertIn("Future<void> main() async {", source)
        self.assertIn("await Firebase.initializeApp();", source)
        self.assertIn("await PushService.initialise();", source)

    def test_main_stays_synchronous_without_push(self):
        self.assertIn("void main() {", template.main_dart(make()))

    def test_the_background_handler_is_a_top_level_entry_point(self):
        # Inside the state class it compiles and then does nothing when the
        # app is terminated, because that isolate has no widget tree.
        source = self.dart()
        self.assertIn("@pragma('vm:entry-point')", source)
        self.assertIn("Future<void> pushBackgroundHandler(RemoteMessage", source)

    def test_the_router_only_accepts_actions_it_knows(self):
        # A payload arrives from the network and must not name arbitrary code.
        self.assertIn("_actions = <String>{'open_url', 'none'}", self.dart())

    def test_the_router_checks_the_destination_domain(self):
        source = self.dart()
        self.assertIn("allowedDomains.any(", source)
        self.assertIn("PushRouter", source)

    def test_topics_carry_their_defaults(self):
        source = self.dart(push_topics=(
            {"id": "general", "label": "General", "default": True},
            {"id": "offers", "label": "Offers", "default": False},
        ))
        self.assertIn('"id": "general", "label": "General", "default": true', source)
        self.assertIn('"default": false', source)

    def test_the_prompt_text_is_the_configured_one(self):
        source = self.dart(push_prompt_title="Hear first",
                           push_prompt_body="Order updates and offers.")
        self.assertIn('const pushPromptTitle = "Hear first";', source)
        self.assertIn("Order updates and offers.", source)

    def test_the_prompt_has_a_default_when_nothing_is_set(self):
        self.assertIn('const pushPromptTitle = "Stay updated";', self.dart())

    def test_a_foreground_notification_uses_the_local_plugin(self):
        source = self.dart(push_foreground="notification")
        self.assertIn("_local.show(", source)
        self.assertNotIn("showMaterialBanner", source)

    def test_a_foreground_banner_needs_a_messenger_key(self):
        source = self.dart(push_foreground="banner")
        self.assertIn("final pushMessengerKey", source)
        self.assertIn("scaffoldMessengerKey: pushMessengerKey", source)
        self.assertIn("showMaterialBanner", source)

    def test_silent_shows_nothing_at_all(self):
        source = self.dart(push_foreground="silent")
        self.assertNotIn("_local.show(", source)
        self.assertNotIn("showMaterialBanner", source)

    def test_no_endpoint_means_the_app_never_phones_anywhere(self):
        self.assertIn('const pushTokenEndpoint = "";', self.dart())

    def test_an_endpoint_is_posted_to(self):
        source = self.dart(push_token_endpoint="https://portal.cissytech.com/t")
        self.assertIn('const pushTokenEndpoint = "https://portal.cissytech.com/t";',
                      source)
        self.assertIn("postUrl(Uri.parse(pushTokenEndpoint))", source)

    def test_the_prompt_waits_for_a_page_to_load(self):
        # Both platforms give an app one chance, and a refusal is permanent.
        source = self.dart()
        self.assertIn("await _maybeAskAboutPush();", source)
        self.assertIn("bool pushPromptShown = false;", source)

    def test_push_adds_the_packages_it_needs(self):
        pubspec = template.pubspec(make(push_enabled=True))
        for package in ("firebase_core", "firebase_messaging",
                        "flutter_local_notifications", "shared_preferences"):
            self.assertIn(package, pubspec)

    def test_push_asks_for_the_android_13_permission(self):
        # Without it the app never gets to ask, and every message is dropped.
        manifest = template.android_manifest(make(push_enabled=True))
        self.assertIn("android.permission.POST_NOTIFICATIONS", manifest)

    def test_push_declares_a_default_channel(self):
        # A message arriving before the app has ever run needs somewhere to go,
        # and no Dart has executed to create a channel at that point.
        manifest = template.android_manifest(make(push_enabled=True))
        self.assertIn("default_notification_channel_id", manifest)

    def test_none_of_that_appears_without_push(self):
        manifest = template.android_manifest(make())
        self.assertNotIn("POST_NOTIFICATIONS", manifest)
        self.assertNotIn("firebase", template.pubspec(make()))

    def test_push_opens_the_bridge_even_without_share_or_location(self):
        # Strategy B is the cheap one: the site reads the token from a page
        # where it already knows who is signed in.
        source = self.dart()
        self.assertIn("addJavaScriptHandler", source)
        self.assertIn("getPushToken", source)
        self.assertIn("requestNotificationPermission", source)

    def test_the_push_bridge_is_still_origin_gated(self):
        source = self.dart()
        self.assertIn("_bridgeOriginAllowed", source)
        self.assertIn("'status': 'denied', 'message': 'Origin is not allowed.'",
                      source)

    def test_no_bridge_at_all_without_push_share_or_location(self):
        self.assertNotIn("addJavaScriptHandler", template.main_dart(make()))


class StatusBarIconTest(unittest.TestCase):
    """The notification icon in the status bar.

    Android keeps only the alpha channel of a small icon, so shipping the
    launcher icon there renders as a featureless square. With a logo uploaded,
    everything points at the silhouette a build-time tool traces from it; with
    no logo there is nothing to trace, and the launcher icon at least exists.
    """

    ICON = "assets/icon/logo.png"

    def dart(self, icon=ICON, **overrides) -> str:
        base = dict(push_enabled=True)
        base.update(overrides)
        return template.main_dart(make(**base), None, None, icon)

    def test_with_a_logo_the_silhouette_is_the_small_icon(self):
        source = self.dart()
        self.assertIn(
            f"AndroidInitializationSettings('@drawable/{template.STAT_ICON}')",
            source,
        )

    def test_without_a_logo_the_launcher_icon_still_exists(self):
        source = self.dart(icon=None)
        self.assertIn("AndroidInitializationSettings('@mipmap/ic_launcher')", source)

    def test_the_manifest_names_the_icon_for_background_messages(self):
        # FCM displays those itself, so no Dart gets a say in the icon.
        manifest = template.android_manifest(
            make(push_enabled=True), has_push_icon=True
        )
        self.assertIn("default_notification_icon", manifest)
        self.assertIn(f"@drawable/{template.STAT_ICON}", manifest)

    def test_the_manifest_stays_quiet_without_a_logo(self):
        manifest = template.android_manifest(make(push_enabled=True))
        self.assertNotIn("default_notification_icon", manifest)
        self.assertNotIn("default_notification_color", manifest)

    def test_the_accent_colour_follows_the_theme(self):
        manifest = template.android_manifest(
            make(push_enabled=True, theme_color="#b3561d"), has_push_icon=True
        )
        self.assertIn("default_notification_color", manifest)
        self.assertIn(f"@color/{template.NOTIFICATION_COLOUR}", manifest)
        resource = template.notification_colour_resource("#b3561d")
        self.assertIn("#b3561d", resource)
        self.assertIn(template.NOTIFICATION_COLOUR, resource)

    def test_no_theme_colour_means_no_colour_meta_data(self):
        manifest = template.android_manifest(
            make(push_enabled=True), has_push_icon=True
        )
        self.assertNotIn("default_notification_color", manifest)

    def test_the_tracing_tool_bakes_in_the_logo_and_every_density(self):
        tool = template.notification_icon_tool(self.ICON)
        self.assertIn(f'const source = "{self.ICON}";', tool)
        self.assertIn(template.STAT_ICON, tool)
        for density in ("mdpi", "hdpi", "xhdpi", "xxhdpi", "xxxhdpi"):
            self.assertIn(density, tool)

    def test_push_with_a_logo_adds_the_image_library(self):
        # What the tracing tool decodes the logo with, dev-only.
        pubspec = template.pubspec(make(push_enabled=True), None, self.ICON)
        dev_block = pubspec.split("dev_dependencies:")[1].split("\nflutter:")[0]
        self.assertIn("image:", dev_block)

    def test_no_logo_or_no_push_means_no_image_library(self):
        self.assertNotIn("\n  image:", template.pubspec(make(push_enabled=True)))
        self.assertNotIn("\n  image:", template.pubspec(make(), None, self.ICON))
