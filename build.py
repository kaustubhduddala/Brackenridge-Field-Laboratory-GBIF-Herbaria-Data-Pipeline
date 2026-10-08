import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "GBIF Herbaria Pipeline"
SLUG = "GBIF-Herbaria-Pipeline"
BUNDLE_ID = "edu.utexas.gbif-herbaria-pipeline"
HIDDEN_IMPORTS = ["program.gui", "program.cli", "bs4", "PIL._tkinter_finder"]
ENTITLEMENTS = ROOT / "assets" / "entitlements.plist"
MACHO_MAGIC = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce",
               b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}

USAGE = """Usage:
  python build.py VERSION                 build, sign if configured, and zip
  python build.py VERSION --no-archive    build and sign only
  python build.py VERSION --archive-only  zip an existing build

macOS signing runs when MACOS_SIGN_IDENTITY is set, and notarization when
APPLE_ID, APPLE_TEAM_ID and APPLE_APP_PASSWORD are also set."""


def platform_tag():
    if sys.platform.startswith("win"):
        return "windows-x64"
    if sys.platform == "darwin":
        return "macos-arm64" if platform.machine() == "arm64" else "macos-intel"
    return f"linux-{platform.machine()}"


def built_app():
    if sys.platform == "darwin":
        return ROOT / "dist" / f"{NAME}.app"
    return ROOT / "dist" / NAME


def run(args, **kwargs):
    print("+", " ".join(str(a) for a in args), flush=True)
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def build(version):
    import PyInstaller.__main__

    (ROOT / "VERSION").write_text(version)
    args = [
        str(ROOT / "main.py"),
        "--name", NAME,
        "--windowed",
        "--onedir",
        "--noconfirm",
        "--clean",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        "--add-data", f"{ROOT / 'VERSION'}{os.pathsep}.",
        "--collect-submodules", "pygbif",
        "--collect-data", "pygbif",
        "--collect-submodules", "keyring",
    ]
    for module in HIDDEN_IMPORTS:
        args += ["--hidden-import", module]
    icon = ROOT / "assets" / ("icon.ico" if sys.platform.startswith("win") else "icon.icns")
    if icon.exists():
        args += ["--icon", str(icon)]
    if sys.platform == "darwin":
        args += ["--osx-bundle-identifier", BUNDLE_ID]
    PyInstaller.__main__.run(args)


def _is_macho(path):
    try:
        with open(path, "rb") as handle:
            return handle.read(4) in MACHO_MAGIC
    except OSError:
        return False


def _codesign(path, identity, entitlements=False):
    args = ["codesign", "--force", "--timestamp", "--options", "runtime", "--sign", identity]
    if entitlements:
        args += ["--entitlements", ENTITLEMENTS]
    run(args + [path])


def sign_macos(app, identity):
    print(f"Signing {app.name} as {identity}")
    binaries = [p for p in app.rglob("*") if p.is_file() and not p.is_symlink() and _is_macho(p)]
    main_executable = app / "Contents" / "MacOS" / NAME
    for path in sorted(binaries, key=lambda p: len(p.parts), reverse=True):
        if path != main_executable:
            _codesign(path, identity)
    frameworks = [p for p in app.rglob("*.framework") if p.is_dir() and not p.is_symlink()]
    for path in sorted(frameworks, key=lambda p: len(p.parts), reverse=True):
        _codesign(path, identity)
    _codesign(app, identity, entitlements=True)
    run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", app])


def notarize_macos(app):
    apple_id, team_id, password = (os.environ.get(k, "") for k in ("APPLE_ID", "APPLE_TEAM_ID", "APPLE_APP_PASSWORD"))
    if not (apple_id and team_id and password):
        print("Skipping notarization: APPLE_ID, APPLE_TEAM_ID and APPLE_APP_PASSWORD are not all set")
        return
    with tempfile.TemporaryDirectory() as tmp:
        upload = Path(tmp) / "notarize.zip"
        run(["ditto", "-c", "-k", "--keepParent", app, upload])
        print("Submitting to Apple for notarization; this usually takes a few minutes")
        result = subprocess.run(["xcrun", "notarytool", "submit", str(upload), "--apple-id", apple_id,
                                 "--team-id", team_id, "--password", password, "--wait",
                                 "--timeout", "45m", "--output-format", "json"],
                                capture_output=True, text=True)
        try:
            outcome = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise SystemExit(f"Notarization failed:\n{result.stdout}\n{result.stderr}")
        if outcome.get("status") != "Accepted":
            submission = outcome.get("id", "")
            if submission:
                subprocess.run(["xcrun", "notarytool", "log", submission, "--apple-id", apple_id,
                                "--team-id", team_id, "--password", password])
            raise SystemExit(f"Notarization was not accepted: {outcome.get('status')} {outcome.get('message', '')}")
    run(["xcrun", "stapler", "staple", app])
    run(["spctl", "--assess", "--type", "execute", "--verbose=2", app])


def archive(version):
    app = built_app()
    if not app.exists():
        raise SystemExit(f"{app} not found; build first")
    target = ROOT / "dist" / f"{SLUG}-{version}-{platform_tag()}.zip"
    target.unlink(missing_ok=True)
    if sys.platform == "darwin":
        run(["ditto", "-c", "-k", "--keepParent", app, target])
    else:
        shutil.make_archive(str(target.with_suffix("")), "zip", app.parent, app.name)
    print(f"Built {target}")


def main(argv):
    flags = {a for a in argv if a.startswith("--")}
    positional = [a for a in argv if not a.startswith("--")]
    if "--help" in flags or flags - {"--no-archive", "--archive-only"}:
        print(USAGE)
        return
    version = positional[0] if positional else "dev"

    if "--archive-only" not in flags:
        build(version)
        identity = os.environ.get("MACOS_SIGN_IDENTITY", "")
        if sys.platform == "darwin" and identity:
            sign_macos(built_app(), identity)
            notarize_macos(built_app())
    if "--no-archive" not in flags:
        archive(version)


if __name__ == "__main__":
    main(sys.argv[1:])