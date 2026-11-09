"""Updates from GitHub releases: find a newer release, download it, check it, and swap it in when the app quits.

Releases are published by .github/workflows/release.yml as Graas-<tag>-<platform>.zip (.tar.gz on Linux), with a
SHA256SUMS.txt beside them. Only the packaged app can replace itself; from source, the check just reports.

The steps, each small enough to test on its own:
    latest_release()     ask the GitHub API for the newest release and pick the file for this computer
    download()           fetch the file over HTTPS and check its SHA-256 against the release's checksum
    stage()              unpack beside the installed app and run the new app's --self-test before trusting it
    apply_on_exit()      start a helper that waits for this app to quit, swaps the folders, and starts the new app
"""
import hashlib
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import requests

from config import APP_NAME, APP_VERSION, FROZEN, UPDATE_REPO

API_URL = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
DOWNLOAD_PREFIX = f"https://github.com/{UPDATE_REPO}/releases/download/"
RELEASES_URL = f"https://github.com/{UPDATE_REPO}/releases/latest"
CHECKSUMS_NAME = "SHA256SUMS.txt"
SELF_TEST_SECONDS = 180
MAX_NOTES = 1200


class UpdateError(Exception):
    """Something went wrong while updating; the message is written for the person using the app."""


@dataclass
class Update:
    tag: str                 # for example "v1.0.14"
    asset_name: str
    url: str
    size: int
    sha256: str              # "" when the release gives no checksum
    notes: str
    page: str                # the release's page on GitHub


def platform_tag():
    """The part of a release file's name that says which computer it is for."""
    if sys.platform.startswith("win"):
        return "windows-x64"
    if sys.platform == "darwin":
        return "macos-arm64" if platform.machine() == "arm64" else "macos-intel"
    machine = platform.machine().lower()
    return "linux-" + {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64"}.get(machine, machine)


def archive_suffix():
    return ".tar.gz" if sys.platform.startswith("linux") else ".zip"


def parse_version(text):
    """(1, 0, 14) from "v1.0.14"; None for "dev", a pre-release such as "v2.0.0-beta1", or anything else."""
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", (text or "").strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def is_newer(tag, current=APP_VERSION):
    """True when `tag` is a newer release than `current`. An unreadable current version (a source checkout,
    "dev") is never out of date: those are updated with git."""
    latest, have = parse_version(tag), parse_version(current)
    if latest is None or have is None:
        return False
    width = max(len(latest), len(have))
    return latest + (0,) * (width - len(latest)) > have + (0,) * (width - len(have))


def _headers():
    return {"Accept": "application/vnd.github+json", "User-Agent": f"{APP_NAME}/{APP_VERSION}"}


def pick_asset(release):
    """(asset, checksum asset or None) for this computer from a release's JSON, or (None, None)."""
    tag = release.get("tag_name", "")
    wanted = f"{APP_NAME}-{tag}-{platform_tag()}{archive_suffix()}"
    assets = release.get("assets") or []
    found = next((a for a in assets if a.get("name") == wanted), None)
    sums = next((a for a in assets if a.get("name") == CHECKSUMS_NAME), None)
    return found, sums


def _sha256_from_sums(text, name):
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    return ""


def latest_release(session=None, timeout=15):
    """The newest published release as an Update for this computer, or None when there is no release or none
    for this computer. Raises UpdateError when GitHub cannot be reached."""
    get = (session or requests).get
    try:
        response = get(API_URL, headers=_headers(), timeout=timeout)
        if response.status_code == 404:
            return None                                         # no release yet (or the repository is private)
        response.raise_for_status()
        release = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise UpdateError(f"Could not reach GitHub to look for updates ({exc.__class__.__name__}).") from exc
    asset, sums = pick_asset(release)
    if asset is None:
        return None
    digest = str(asset.get("digest") or "")
    sha = digest.split(":", 1)[1].lower() if digest.lower().startswith("sha256:") else ""
    if not sha and sums and str(sums.get("browser_download_url", "")).startswith(DOWNLOAD_PREFIX):
        try:
            reply = get(sums["browser_download_url"], headers=_headers(), timeout=timeout)
            reply.raise_for_status()
            sha = _sha256_from_sums(reply.text, asset["name"])
        except requests.RequestException:
            sha = ""
    return Update(tag=release.get("tag_name", ""), asset_name=asset["name"], url=asset.get("browser_download_url", ""),
                  size=int(asset.get("size") or 0), sha256=sha, notes=(release.get("body") or "").strip()[:MAX_NOTES],
                  page=release.get("html_url") or RELEASES_URL)


def check(session=None):
    """The Update to offer, or None when this version is current (or cannot be compared)."""
    update = latest_release(session)
    return update if update and is_newer(update.tag) else None


# ------------------------------------------------------------------------------------------------ download

def download(update, folder, progress=None, cancelled=None, session=None):
    """Download the release file into `folder` and check it; returns its path. `progress(done, total)` is called
    as bytes arrive, and `cancelled()` returning True stops the download."""
    if not update.url.startswith(DOWNLOAD_PREFIX):
        raise UpdateError("The release file is not on the expected GitHub address, so it was not downloaded.")
    if not update.sha256:
        raise UpdateError("This release has no checksum to verify the download against, so it will not be "
                          "installed automatically. Download it from the releases page instead.")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / update.asset_name
    digest, done = hashlib.sha256(), 0
    try:
        with (session or requests).get(update.url, headers={"User-Agent": _headers()["User-Agent"]}, stream=True,
                                       timeout=30) as response:
            response.raise_for_status()
            total = int(response.headers.get("Content-Length") or update.size or 0)
            with open(target, "wb") as out:
                for chunk in response.iter_content(1 << 20):
                    if cancelled and cancelled():
                        raise UpdateError("The download was cancelled.")
                    out.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
    except requests.RequestException as exc:
        target.unlink(missing_ok=True)
        raise UpdateError(f"The download failed ({exc.__class__.__name__}).") from exc
    except UpdateError:
        target.unlink(missing_ok=True)
        raise
    if digest.hexdigest() != update.sha256:
        target.unlink(missing_ok=True)
        raise UpdateError("The downloaded file did not match its checksum, so it was thrown away.")
    return target


# ------------------------------------------------------------------------------------------------ installing

def install_location():
    """The installed app that would be replaced: the .app bundle on macOS, the app folder elsewhere. None when
    this is not a packaged app."""
    if not FROZEN:
        return None
    exe = Path(sys.executable).resolve()
    if sys.platform == "darwin":
        bundle = exe.parents[2]
        return bundle if bundle.suffix == ".app" else None
    return exe.parent


def new_executable(root):
    """The program inside an unpacked copy of the app."""
    if sys.platform == "darwin":
        return root / "Contents" / "MacOS" / APP_NAME
    return root / (APP_NAME + (".exe" if sys.platform.startswith("win") else ""))


def _unpack(archive, folder):
    """Unpack a release file into `folder`; returns the unpacked app (the one folder or .app inside it)."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":                  # ditto keeps the symlinks, permissions and code signature
        subprocess.run(["ditto", "-x", "-k", str(archive), str(folder)], check=True)
    elif str(archive).endswith(".tar.gz"):
        with tarfile.open(archive) as tar:
            tar.extractall(folder, filter="data")
    else:
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(folder)
    top = [p for p in folder.iterdir() if not p.name.startswith(".")]
    if len(top) != 1:
        raise UpdateError("The downloaded update does not look like the app.")
    return top[0]


def stage(archive, install):
    """Unpack the download next to the installed app (so swapping them is a rename) and check that the new app
    starts on this computer. Returns the staging folder and the unpacked app inside it."""
    parent = Path(install).parent
    if not os.access(parent, os.W_OK):
        raise UpdateError(f"The app cannot be replaced because {parent} is not writable by you. Download the new "
                          "version from the releases page and install it yourself.")
    staging = Path(tempfile.mkdtemp(prefix=f".{APP_NAME}-update-", dir=parent))
    try:
        unpacked = _unpack(archive, staging)
        exe = new_executable(unpacked)
        if not exe.is_file():
            raise UpdateError("The downloaded update does not contain the app.")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        result = subprocess.run([str(exe), "--self-test"], capture_output=True, text=True, timeout=SELF_TEST_SECONDS)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()[-1:] or [""]
            raise UpdateError(f"The new version did not start on this computer, so it was not installed. {detail[0]}")
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError("The new version took too long to start, so it was not installed.") from exc
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging, unpacked


POSIX_SCRIPT = """#!/bin/sh
# Waits for the app to quit, swaps in the new version, and starts it. Puts the old version back if that fails.
pid="$1"; app="$2"; new="$3"; staging="$4"; start="$5"
while kill -0 "$pid" 2>/dev/null; do sleep 1; done
old="$app.old-$$"
if mv "$app" "$old" && mv "$new" "$app"; then
    rm -rf "$old" "$staging"
else
    [ -e "$old" ] && [ ! -e "$app" ] && mv "$old" "$app"
fi
case "$start" in
    open) open "$app" ;;
    *) "$app/{name}" >/dev/null 2>&1 & ;;
esac
rm -f "$0"
"""

WINDOWS_SCRIPT = """@echo off
rem Waits for the app to quit, swaps in the new version, and starts it. Puts the old version back if that fails.
set PID=%~1
set APP=%~2
set NEW=%~3
set STAGING=%~4
:wait
tasklist /FI "PID eq %PID%" 2>nul | find "%PID%" >nul
if not errorlevel 1 (
    ping -n 2 127.0.0.1 >nul
    goto wait
)
set OLD=%APP%.old
if exist "%OLD%" rmdir /s /q "%OLD%"
move "%APP%" "%OLD%" >nul
if errorlevel 1 goto start
move "%NEW%" "%APP%" >nul
if errorlevel 1 (
    move "%OLD%" "%APP%" >nul
    goto start
)
rmdir /s /q "%OLD%"
rmdir /s /q "%STAGING%"
:start
start "" "%APP%\\{name}.exe"
del "%~f0"
"""


def apply_on_exit(install, unpacked, staging):
    """Start the helper that swaps the staged app in once this process has exited. The caller then quits."""
    scripts = Path(tempfile.mkdtemp(prefix=f"{APP_NAME}-updater-"))
    if sys.platform.startswith("win"):
        script = scripts / "update.bat"
        script.write_text(WINDOWS_SCRIPT.format(name=APP_NAME), encoding="utf-8")
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        subprocess.Popen(["cmd", "/c", str(script), str(os.getpid()), str(install), str(unpacked), str(staging)],
                         creationflags=flags, close_fds=True)
    else:
        script = scripts / "update.sh"
        script.write_text(POSIX_SCRIPT.replace("{name}", APP_NAME), encoding="utf-8")
        script.chmod(0o700)
        subprocess.Popen(["/bin/sh", str(script), str(os.getpid()), str(install), str(unpacked), str(staging),
                          "open" if sys.platform == "darwin" else "run"],
                         start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, close_fds=True)
