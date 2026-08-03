import json
import shutil
import tempfile
import unittest
from pathlib import Path

from cissy.config import AppConfig
from cissy.errors import NotFoundError, ValidationError
from cissy.store import ProjectStore


def make(**overrides) -> AppConfig:
    base = dict(
        id="",
        name="Cissytech Portal",
        website_url="https://portal.cissytech.com",
        android_package_id="com.cissytech.portal",
        ios_bundle_id="com.cissytech.portal",
    )
    base.update(overrides)
    return AppConfig(**base)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cissy_test_"))
        self.store = ProjectStore(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_create_derives_an_id_from_the_name(self):
        app = self.store.create(make())
        self.assertEqual(app.id, "cissytech-portal")
        self.assertTrue((self.root / "cissytech-portal" / "config.json").is_file())

    def test_two_apps_with_the_same_name_get_distinct_ids(self):
        first = self.store.create(make())
        second = self.store.create(make())
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(second.id, "cissytech-portal-2")

    def test_get_reads_back_what_was_written(self):
        created = self.store.create(make(features=("Camera",)))
        self.assertEqual(self.store.get(created.id), created)

    def test_save_refuses_an_app_that_does_not_exist(self):
        with self.assertRaises(NotFoundError):
            self.store.save(make(id="ghost"))

    def test_list_skips_a_corrupt_config_instead_of_failing(self):
        good = self.store.create(make())
        broken = self.root / "broken"
        broken.mkdir()
        (broken / "config.json").write_text("{not json", encoding="utf-8")

        listed = [app.id for app in self.store.list()]
        self.assertEqual(listed, [good.id])

    def test_delete_removes_everything(self):
        app = self.store.create(make())
        self.store.delete(app.id)
        self.assertFalse((self.root / app.id).exists())

    def test_an_id_cannot_escape_the_projects_directory(self):
        # The id comes straight from the URL, so this is the load-bearing check.
        for hostile in ("../secrets", "..", "a/b", "/etc/passwd"):
            with self.subTest(hostile=hostile):
                with self.assertRaises(ValidationError):
                    self.store.app_dir(hostile)

    def test_a_partial_write_cannot_truncate_the_config(self):
        app = self.store.create(make())
        path = self.root / app.id / "config.json"
        before = json.loads(path.read_text(encoding="utf-8"))

        self.store.save(app.updated(app_name="Renamed"))

        after = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(after["app_name"], "Renamed")
        self.assertEqual(after["created_at"], before["created_at"])
        self.assertFalse(list(path.parent.glob("*.tmp")))


class DuplicateTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cissy_test_"))
        self.store = ProjectStore(self.root)
        self.source = self.store.create(
            make(features=("Camera", "Downloads"), allowed_domains=("a.com",))
        )

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_copies_the_settings(self):
        copy = self.store.duplicate(self.source.id, "Kampala Shop")
        self.assertEqual(copy.features, self.source.features)
        self.assertEqual(copy.allowed_domains, self.source.allowed_domains)
        self.assertEqual(copy.name, "Kampala Shop")
        self.assertEqual(copy.id, "kampala-shop")

    def test_does_not_inherit_the_signing_key(self):
        signed = self.store.save(
            self.source.updated(keystore_file="upload.jks", key_alias="upload")
        )
        copy = self.store.duplicate(signed.id, "Other App")
        self.assertIsNone(copy.keystore_file)
        self.assertIsNone(copy.key_alias)

    def test_restarts_the_version_numbering(self):
        bumped = self.store.save(self.source.updated(version_code=12))
        copy = self.store.duplicate(bumped.id, "Fresh App")
        self.assertEqual(copy.version_code, 1)
        self.assertEqual(copy.version_name, "1.0.0")

    def test_copies_asset_files(self):
        assets = self.store.assets_dir(self.source.id)
        (assets / "icon.png").write_bytes(b"png-bytes")
        with_icon = self.store.save(self.source.updated(icon_file="icon.png"))

        copy = self.store.duplicate(with_icon.id, "With Icon")
        copied = self.store.assets_dir(copy.id) / "icon.png"
        self.assertTrue(copied.is_file())
        self.assertEqual(copied.read_bytes(), b"png-bytes")


class BuildNumberTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cissy_test_"))
        self.store = ProjectStore(self.root)
        self.app = self.store.create(make())

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_starts_at_one(self):
        self.assertEqual(self.store.next_build_number(self.app.id), 1)

    def test_continues_past_the_highest_existing(self):
        for number in (1, 2, 7):
            self.store.build_dir(self.app.id, number).mkdir(parents=True)
        self.assertEqual(self.store.next_build_number(self.app.id), 8)
        self.assertEqual(self.store.build_numbers(self.app.id), [7, 2, 1])

    def test_ignores_directories_that_are_not_numbers(self):
        (self.store.builds_dir(self.app.id)).mkdir(parents=True)
        (self.store.builds_dir(self.app.id) / "scratch").mkdir()
        self.assertEqual(self.store.next_build_number(self.app.id), 1)


if __name__ == "__main__":
    unittest.main()
