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
