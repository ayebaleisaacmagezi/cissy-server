import unittest

from cissy.config import AppConfig, normalise, slugify, validate
from cissy.errors import ValidationError


def make(**overrides) -> AppConfig:
    base = dict(
        id="portal",
        name="Cissytech Portal",
        website_url="https://portal.cissytech.com",
        android_package_id="com.cissytech.portal",
        ios_bundle_id="com.cissytech.portal",
    )
    base.update(overrides)
    return AppConfig(**base)


class SlugTest(unittest.TestCase):
    def test_makes_a_directory_safe_id(self):
        self.assertEqual(slugify("Cissytech Portal"), "cissytech-portal")
        self.assertEqual(slugify("  Shop!! 2024  "), "shop-2024")

    def test_never_returns_empty(self):
        # An empty id would resolve to the projects directory itself.
        self.assertEqual(slugify("!!!"), "app")
        self.assertEqual(slugify(""), "app")


class ValidateTest(unittest.TestCase):
    def test_accepts_a_sound_config(self):
        validate(make())

    def test_rejects_a_url_without_a_scheme(self):
        with self.assertRaises(ValidationError) as caught:
            validate(make(website_url="portal.cissytech.com"))
        self.assertIn("http://", str(caught.exception))

    def test_rejects_http_when_https_is_required(self):
        with self.assertRaises(ValidationError):
            validate(make(website_url="http://portal.cissytech.com"))

    def test_allows_http_when_https_is_not_required(self):
        validate(make(website_url="http://internal.lan", require_https=False))

    def test_rejects_a_single_segment_package_id(self):
        with self.assertRaises(ValidationError):
            validate(make(android_package_id="portal"))

    def test_rejects_a_package_id_starting_with_a_digit(self):
        # Gradle rejects this too, but hours later and less clearly.
        with self.assertRaises(ValidationError):
            validate(make(android_package_id="com.2cissy.portal"))

    def test_rejects_an_unknown_feature(self):
        with self.assertRaises(ValidationError) as caught:
            validate(make(features=("Teleportation",)))
        self.assertIn("Teleportation", str(caught.exception))

    def test_rejects_a_version_name_that_is_not_numeric(self):
        with self.assertRaises(ValidationError):
            validate(make(version_name="v1.4"))

    def test_rejects_a_version_code_below_one(self):
        with self.assertRaises(ValidationError):
            validate(make(version_code=0))


class NormaliseTest(unittest.TestCase):
    def test_lowercases_and_deduplicates_domains(self):
        cleaned = normalise(
            make(allowed_domains=("Portal.Cissytech.com", "portal.cissytech.com "))
        )
        self.assertEqual(cleaned.allowed_domains, ("portal.cissytech.com",))

    def test_drops_unknown_features_and_keeps_a_stable_order(self):
        cleaned = normalise(make(features=("Camera", "Downloads", "Nonsense")))
        self.assertEqual(cleaned.features, ("Downloads", "Camera"))

    def test_turns_a_blank_user_agent_into_none(self):
        self.assertIsNone(normalise(make(custom_user_agent="   ")).custom_user_agent)


class SerialisationTest(unittest.TestCase):
    def test_round_trips(self):
        original = make(features=("Camera",), allowed_domains=("a.com",))
        restored = AppConfig.from_json(original.to_json())
        self.assertEqual(restored, original)

    def test_never_writes_a_password_field(self):
        # The whole signing design rests on this staying true.
        payload = make(keystore_file="upload.jks", key_alias="upload").to_json()
        for key in payload:
            self.assertNotIn("password", key.lower())

    def test_refuses_a_newer_schema(self):
        payload = make().to_json()
        payload["schema_version"] = 99
        with self.assertRaises(ValidationError):
            AppConfig.from_json(payload)

    def test_survives_a_hand_edited_file_with_wrong_types(self):
        payload = make().to_json()
        payload["features"] = "Camera"  # a string, not a list
        payload["version_code"] = "twelve"
        restored = AppConfig.from_json(payload)
        self.assertEqual(restored.features, ())
        self.assertEqual(restored.version_code, 1)


if __name__ == "__main__":
    unittest.main()

class CustomOfflineHtmlTest(unittest.TestCase):
    """The developer's own offline screen travels inside the config."""

    def test_round_trips_through_json(self):
        config = make(offline_custom_html="<h1>Offline</h1>")
        self.assertEqual(
            AppConfig.from_json(config.to_json()).offline_custom_html,
            "<h1>Offline</h1>",
        )

    def test_normalise_strips_the_edges(self):
        config = normalise(make(offline_custom_html="  <h1>Hi</h1>\n"))
        self.assertEqual(config.offline_custom_html, "<h1>Hi</h1>")

    def test_accepts_a_reasonable_screen(self):
        validate(make(offline_custom_html="<h1>Offline</h1>"))

    def test_rejects_an_oversized_screen(self):
        # It ships inside every APK the app builds.
        with self.assertRaises(ValidationError) as caught:
            validate(make(offline_custom_html="x" * 200_001))
        self.assertIn("200", str(caught.exception))


class WebsiteNavigationTest(unittest.TestCase):
    """Hiding the site's own navigation, so a native bar is not a second one."""

    def test_accepts_ordinary_selectors(self):
        validate(make(hide_selectors=(".mobile-nav", "#site-header .menu")))

    def test_rejects_a_selector_carrying_a_rule_body(self):
        # The selector ends up inside a CSS rule, inside a JavaScript string,
        # inside a Dart string. A brace escapes the first of those.
        with self.assertRaises(ValidationError) as caught:
            validate(make(hide_selectors=(".nav { display: block }",)))
        self.assertIn("braces", str(caught.exception))

    def test_rejects_every_character_that_could_break_out(self):
        for selector in ('.a"b', ".a'b", ".a;b", ".a\b", ".a<b", ".a>b", ".a`b"):
            with self.subTest(selector=selector):
                with self.assertRaises(ValidationError):
                    validate(make(hide_selectors=(selector,)))

    def test_rejects_a_selector_spanning_lines(self):
        with self.assertRaises(ValidationError):
            validate(make(hide_selectors=(".nav\n.menu",)))

    def test_rejects_an_overlong_selector(self):
        with self.assertRaises(ValidationError):
            validate(make(hide_selectors=("." + "a" * 130,)))

    def test_rejects_too_many_selectors(self):
        with self.assertRaises(ValidationError):
            validate(make(hide_selectors=tuple(f".n{i}" for i in range(21))))

    def test_normalise_keeps_selector_case(self):
        # Class names are case-sensitive to a browser, unlike domains.
        config = normalise(make(hide_selectors=("  .mobileNav  ", ".mobileNav")))
        self.assertEqual(config.hide_selectors, (".mobileNav",))

    def test_accepts_a_body_class(self):
        validate(make(body_class="web2app-native"))

    def test_rejects_a_body_class_that_is_not_one_name(self):
        for value in ("web2app native", ".web2app", "2app", "app!"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    validate(make(body_class=value))

    def test_accepts_a_url_flag(self):
        validate(make(url_flag="source=web2app"))

    def test_rejects_a_url_flag_that_is_not_a_pair(self):
        for value in ("source", "source=", "a=b&c=d"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    validate(make(url_flag=value))

    def test_normalise_drops_a_leading_question_mark(self):
        # "?source=web2app" is what somebody copies out of a browser bar.
        self.assertEqual(normalise(make(url_flag="?source=web2app")).url_flag,
                         "source=web2app")

    def test_round_trips_through_json(self):
        config = make(
            hide_selectors=(".mobile-nav",),
            body_class="web2app-native",
            url_flag="source=web2app",
        )
        restored = AppConfig.from_json(config.to_json())
        self.assertEqual(restored.hide_selectors, (".mobile-nav",))
        self.assertEqual(restored.body_class, "web2app-native")
        self.assertEqual(restored.url_flag, "source=web2app")


class TabMatchTest(unittest.TestCase):
    """The extra paths a navigation tab lights up for."""

    def tabs(self, **extra):
        return dict(
            nav_style="bottom",
            nav_tabs=(
                {"label": "Home", "icon": "home", "target": "/"},
                dict({"label": "Account", "icon": "person", "target": "/account"},
                     **extra),
            ),
        )

    def test_accepts_paths(self):
        validate(make(**self.tabs(match=["/profile", "/orders"])))

    def test_a_list_survives_the_json_reader(self):
        # Every other value on a tab is coerced with str(). Doing that here
        # would store "['/profile']", which matches nothing and says nothing.
        config = make(**self.tabs(match=["/profile"]))
        restored = AppConfig.from_json(config.to_json())
        self.assertEqual(restored.nav_tabs[1]["match"], ["/profile"])

    def test_a_non_list_match_is_treated_as_absent(self):
        data = make(**self.tabs()).to_json()
        data["nav_tabs"][1]["match"] = "/profile"
        self.assertEqual(AppConfig.from_json(data).nav_tabs[1]["match"], [])

    def test_rejects_a_match_that_is_not_a_path(self):
        with self.assertRaises(ValidationError) as caught:
            validate(make(**self.tabs(match=["https://elsewhere.example/x"])))
        self.assertIn("starting with /", str(caught.exception))

    def test_rejects_matches_on_a_native_tab(self):
        # A native screen is not showing a page of the website at all.
        with self.assertRaises(ValidationError):
            validate(make(
                features=("Saved items",),
                nav_style="bottom",
                nav_tabs=(
                    {"label": "Home", "icon": "home", "target": "/"},
                    {"label": "Saved", "icon": "bookmark",
                     "target": "native:saved", "match": ["/saved"]},
                ),
            ))

    def test_rejects_too_many_matches(self):
        with self.assertRaises(ValidationError):
            validate(make(**self.tabs(match=[f"/p{i}" for i in range(11)])))

    def test_normalise_strips_and_dedupes(self):
        config = normalise(make(**self.tabs(match=["  /profile ", "/profile", ""])))
        self.assertEqual(config.nav_tabs[1]["match"], ["/profile"])

    def test_tabs_without_matches_still_work(self):
        config = normalise(make(**self.tabs()))
        validate(config)
        self.assertEqual(config.nav_tabs[1]["match"], [])
