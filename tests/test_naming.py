"""The app's name appears in file names, folders, keychain entries and the release workflow: keep them consistent."""
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import build
import config

ROOT = Path(__file__).resolve().parent.parent


class Naming(unittest.TestCase):
    def test_name_is_safe_in_a_folder_name(self):
        for name in (config.APP_NAME, build.NAME, build.SLUG):
            self.assertFalse(re.search(r'[<>:"/\\|?*]', name), name)        # a colon is not allowed on Windows
            self.assertEqual(name, name.strip())
        self.assertEqual(build.NAME, config.APP_NAME)                        # the app, folder and keychain agree
        self.assertIn(config.APP_NAME, config.APP_TITLE)

    def test_workflow_paths_use_the_built_name(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()
        built = set(re.findall(r"dist[\\/]+([^\\/\"'\s]+)", workflow))
        built = {re.sub(r"\.(app|exe)$", "", name) for name in built
                 if not name.startswith("*") and not name.endswith(".txt")}       # release files, not the app
        self.assertEqual(built, {build.NAME})                                # case matters on Linux

    def test_linux_is_built_and_released(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()
        self.assertIn("ubuntu-", workflow)
        self.assertIn("dist/*.tar.gz", workflow)

    def test_platform_tags(self):
        with mock.patch.object(sys, "platform", "linux"), mock.patch("platform.machine", return_value="x86_64"):
            self.assertEqual(build.platform_tag(), "linux-x64")
        with mock.patch.object(sys, "platform", "linux"), mock.patch("platform.machine", return_value="aarch64"):
            self.assertEqual(build.platform_tag(), "linux-arm64")

    def test_entitlements_file_exists_for_macos_signing(self):
        self.assertTrue(build.ENTITLEMENTS.is_file())


class EarlierVersions(unittest.TestCase):
    def test_old_folder_is_kept_until_the_new_one_exists(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(Path, "home", return_value=Path(home)):
            documents = Path(home) / "Documents"
            (documents / config.LEGACY_APP_NAME).mkdir(parents=True)
            self.assertEqual(config._packaged_home(), documents / config.LEGACY_APP_NAME)
            (documents / config.APP_NAME).mkdir()
            self.assertEqual(config._packaged_home(), documents / config.APP_NAME)

    def test_new_install_uses_the_new_folder(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(Path, "home", return_value=Path(home)):
            self.assertEqual(config._packaged_home(), Path(home) / "Documents" / config.APP_NAME)

    def test_secret_saved_under_the_old_name_is_found_and_copied(self):
        class Store:
            def __init__(self):
                self.items = {(config.LEGACY_KEYRING_SERVICE, "me"): "secret"}

            def get_password(self, service, name):
                return self.items.get((service, name))

            def set_password(self, service, name, secret):
                self.items[(service, name)] = secret

        store = Store()
        found = config._stored_secret(store, config.KEYRING_SERVICE, config.LEGACY_KEYRING_SERVICE, "me")
        self.assertEqual(found, "secret")
        self.assertEqual(store.items[(config.KEYRING_SERVICE, "me")], "secret")
        self.assertIsNone(config._stored_secret(store, config.KEYRING_SERVICE, config.LEGACY_KEYRING_SERVICE, "x"))


if __name__ == "__main__":
    unittest.main()
