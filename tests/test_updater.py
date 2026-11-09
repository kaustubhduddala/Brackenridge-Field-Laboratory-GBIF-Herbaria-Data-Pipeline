"""The updater: version comparison, choosing and verifying a download, staging it, and swapping it in."""
import hashlib
import io
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import requests

from program import updater

DOWNLOAD = updater.DOWNLOAD_PREFIX + "v1.0.20/"


class FakeResponse:
    def __init__(self, status=200, payload=None, content=b"", text=""):
        self.status_code, self.payload, self.content, self.text = status, payload, content, text
        self.headers = {"Content-Length": str(len(content))}

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def iter_content(self, size):
        for i in range(0, len(self.content), size):
            yield self.content[i:i + size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses, self.requested = responses, []

    def get(self, url, **kwargs):
        self.requested.append(url)
        reply = self.responses[url]
        if isinstance(reply, Exception):
            raise reply
        return reply


def release(digest=""):
    name = f"Graas-v1.0.20-{updater.platform_tag()}{updater.archive_suffix()}"
    return {"tag_name": "v1.0.20", "html_url": "https://github.com/x/y/releases/tag/v1.0.20", "body": "Notes",
            "assets": [{"name": name, "browser_download_url": DOWNLOAD + name, "size": 5, "digest": digest},
                       {"name": updater.CHECKSUMS_NAME, "browser_download_url": DOWNLOAD + updater.CHECKSUMS_NAME,
                        "size": 10}]}, name


class Versions(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(updater.parse_version("v1.0.14"), (1, 0, 14))
        self.assertEqual(updater.parse_version("2.1"), (2, 1))
        for text in ("dev", "dev-abc1234", "v2.0.0-beta1", "", None):
            self.assertIsNone(updater.parse_version(text))

    def test_newer(self):
        self.assertTrue(updater.is_newer("v1.0.13", "v1.0.12"))
        self.assertTrue(updater.is_newer("v1.1", "v1.0.99"))
        self.assertTrue(updater.is_newer("v1.0.10", "v1.0.9"))          # numbers, not text
        self.assertFalse(updater.is_newer("v1.0.12", "v1.0.12"))
        self.assertFalse(updater.is_newer("v1.0.1", "v1.0.12"))
        self.assertFalse(updater.is_newer("v1.0", "v1.0.0"))
        self.assertFalse(updater.is_newer("v2.0.0", "dev"))              # a source checkout is not out of date
        self.assertFalse(updater.is_newer("v2.0.0-beta1", "v1.0.0"))      # pre-releases are not offered


class Checking(unittest.TestCase):
    def test_finds_the_file_for_this_computer_and_its_checksum(self):
        data, name = release(digest="sha256:" + "ab" * 32)
        session = FakeSession({updater.API_URL: FakeResponse(payload=data)})
        update = updater.latest_release(session)
        self.assertEqual((update.tag, update.asset_name, update.sha256), ("v1.0.20", name, "ab" * 32))

    def test_checksum_file_is_used_when_the_release_has_no_digest(self):
        data, name = release()
        sums = f"{'cd' * 32}  {name}\n{'ef' * 32}  other.zip\n"
        session = FakeSession({updater.API_URL: FakeResponse(payload=data),
                               DOWNLOAD + updater.CHECKSUMS_NAME: FakeResponse(text=sums)})
        self.assertEqual(updater.latest_release(session).sha256, "cd" * 32)

    def test_no_release_or_no_file_for_this_computer(self):
        self.assertIsNone(updater.latest_release(FakeSession({updater.API_URL: FakeResponse(status=404)})))
        data, _ = release()
        data["assets"] = [{"name": "Graas-v1.0.20-somewhere-else.zip"}]
        self.assertIsNone(updater.latest_release(FakeSession({updater.API_URL: FakeResponse(payload=data)})))

    def test_unreachable_github_is_a_readable_error(self):
        session = FakeSession({updater.API_URL: requests.ConnectionError("offline")})
        with self.assertRaises(updater.UpdateError):
            updater.latest_release(session)

    def test_check_offers_only_newer_releases(self):
        data, _ = release(digest="sha256:" + "ab" * 32)
        session = FakeSession({updater.API_URL: FakeResponse(payload=data)})
        with mock.patch.object(updater, "is_newer", lambda tag: True):
            self.assertEqual(updater.check(session).tag, "v1.0.20")
        with mock.patch.object(updater, "is_newer", lambda t: False):
            self.assertIsNone(updater.check(session))


class Downloading(unittest.TestCase):
    def update(self, content, sha=None, url=None):
        return updater.Update("v1.0.20", "Graas.zip", url or DOWNLOAD + "Graas.zip", len(content),
                              hashlib.sha256(content).hexdigest() if sha is None else sha, "", "page")

    def test_good_download_is_kept(self):
        content = b"x" * 3_000_000
        update = self.update(content)
        seen = []
        with tempfile.TemporaryDirectory() as folder:
            path = updater.download(update, folder, lambda done, total: seen.append((done, total)),
                                    session=FakeSession({update.url: FakeResponse(content=content)}))
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(seen[-1], (3_000_000, 3_000_000))

    def test_wrong_checksum_is_thrown_away(self):
        update = self.update(b"abc", sha="00" * 32)
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(updater.UpdateError):
            updater.download(update, folder, session=FakeSession({update.url: FakeResponse(content=b"abc")}))
        self.assertFalse((Path(folder) / "Graas.zip").exists())

    def test_refuses_other_addresses_and_missing_checksums(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(updater.UpdateError):
                updater.download(self.update(b"a", url="https://example.com/Graas.zip"), folder)
            with self.assertRaises(updater.UpdateError):
                updater.download(self.update(b"a", sha=""), folder)

    def test_cancel_stops_and_cleans_up(self):
        update = self.update(b"x" * 3_000_000)
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(updater.UpdateError):
            updater.download(update, folder, cancelled=lambda: True,
                            session=FakeSession({update.url: FakeResponse(content=b"x" * 3_000_000)}))
        self.assertFalse((Path(folder) / "Graas.zip").exists())


@unittest.skipIf(sys.platform.startswith("win"), "uses shell scripts as stand-in apps")
class Installing(unittest.TestCase):
    def archive(self, folder, exit_code):
        """A tarball holding Graas/Graas, a script that exits with `exit_code` (a stand-in for --self-test)."""
        app = Path(folder) / "build" / "Graas"
        app.mkdir(parents=True)
        program = app / "Graas"
        program.write_text(f"#!/bin/sh\nexit {exit_code}\n")
        program.chmod(0o755)
        target = Path(folder) / "Graas-v1.0.20-linux-x64.tar.gz"
        with tarfile.open(target, "w:gz") as tar:
            tar.add(app, arcname="Graas")
        return target

    def test_stage_runs_the_new_app_before_trusting_it(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(sys, "platform", "linux"):
            install = Path(folder) / "apps" / "Graas"
            install.mkdir(parents=True)
            staging, unpacked = updater.stage(self.archive(folder, 0), install)
            self.assertEqual(unpacked.name, "Graas")
            self.assertEqual(staging.parent, install.parent)                  # same disk: swapping is a rename
            self.assertTrue((unpacked / "Graas").is_file())

    def test_a_new_app_that_does_not_start_is_not_installed(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(sys, "platform", "linux"):
            install = Path(folder) / "apps" / "Graas"
            install.mkdir(parents=True)
            with self.assertRaises(updater.UpdateError):
                updater.stage(self.archive(folder, 1), install)
            self.assertEqual([p.name for p in install.parent.iterdir()], ["Graas"])     # staging was removed

    def test_unwritable_folder_is_explained(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch("os.access", return_value=False):
            with self.assertRaises(updater.UpdateError):
                updater.stage(Path(folder) / "x.tar.gz", Path(folder) / "Graas")

    @unittest.skipUnless(sys.platform == "darwin", "macOS unpacks with ditto")
    def test_macos_app_bundle_keeps_its_executable_bit_and_is_checked(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            program = root / "build" / "Graas.app" / "Contents" / "MacOS" / "Graas"
            program.parent.mkdir(parents=True)
            program.write_text("#!/bin/sh\nexit 0\n")
            program.chmod(0o755)
            archive = root / "Graas-v1.0.20-macos-arm64.zip"
            subprocess.run(["ditto", "-c", "-k", "--keepParent", str(root / "build" / "Graas.app"), str(archive)],
                           check=True)
            install = root / "apps" / "Graas.app"
            install.mkdir(parents=True)
            staging, unpacked = updater.stage(archive, install)
            self.assertEqual(unpacked.name, "Graas.app")
            self.assertTrue(updater.new_executable(unpacked).stat().st_mode & 0o100)

    def run_swap(self, new_exists):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app, staging = root / "Graas", root / ".Graas-update-1"
            app.mkdir()
            (app / "version").write_text("old")
            new = staging / "Graas"
            if new_exists:
                new.mkdir(parents=True)
                (new / "version").write_text("new")
                program = new / "Graas"
                program.write_text(f"#!/bin/sh\necho started > '{root}/started'\n")
                program.chmod(0o755)
            else:
                staging.mkdir()
            script = root / "update.sh"
            script.write_text(updater.POSIX_SCRIPT.replace("{name}", "Graas"))
            finished = subprocess.Popen(["true"])
            finished.wait()                                     # a process that has already exited
            subprocess.run(["/bin/sh", str(script), str(finished.pid), str(app), str(new), str(staging), "run"],
                           check=True)
            time.sleep(0.5)                                     # the new app is started in the background
            return ((app / "version").read_text(), staging.exists(), (root / "started").exists(),
                    [p.name for p in root.iterdir() if ".old-" in p.name], script.exists())

    def test_swap_replaces_the_app_and_starts_it(self):
        version, staging_left, started, old_left, script_left = self.run_swap(new_exists=True)
        self.assertEqual(version, "new")
        self.assertFalse(staging_left)
        self.assertTrue(started)
        self.assertEqual(old_left, [])
        self.assertFalse(script_left)

    def test_failed_swap_puts_the_old_app_back(self):
        version, _, _, old_left, _ = self.run_swap(new_exists=False)
        self.assertEqual(version, "old")
        self.assertEqual(old_left, [])


if __name__ == "__main__":
    unittest.main()
