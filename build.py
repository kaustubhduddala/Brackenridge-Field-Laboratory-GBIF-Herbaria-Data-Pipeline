import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent
NAME = "GBIF Herbaria Pipeline"
SLUG = "GBIF-Herbaria-Pipeline"
BUNDLE_ID = "edu.utexas.gbif-herbaria-pipeline"
HIDDEN_IMPORTS = ["gui", "cli", "bs4", "PIL._tkinter_finder"]


def platform_tag():
    if sys.platform.startswith("win"):
        return "windows-x64"
    if sys.platform == "darwin":
        return "macos-arm64" if platform.machine() == "arm64" else "macos-intel"
    return f"linux-{platform.machine()}"


def main():
    version = sys.argv[1] if len(sys.argv) > 1 else "dev"
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
    ]
    for module in HIDDEN_IMPORTS:
        args += ["--hidden-import", module]
    icon = ROOT / "assets" / ("icon.ico" if sys.platform.startswith("win") else "icon.icns")
    if icon.exists():
        args += ["--icon", str(icon)]
    if sys.platform == "darwin":
        args += ["--osx-bundle-identifier", BUNDLE_ID]
    PyInstaller.__main__.run(args)

    archive = ROOT / "dist" / f"{SLUG}-{version}-{platform_tag()}.zip"
    archive.unlink(missing_ok=True)
    if sys.platform == "darwin":
        app = ROOT / "dist" / f"{NAME}.app"
        subprocess.run(["ditto", "-c", "-k", "--keepParent", str(app), str(archive)], check=True)
    else:
        shutil.make_archive(str(archive.with_suffix("")), "zip", ROOT / "dist", NAME)
    print(f"Built {archive}")


if __name__ == "__main__":
    main()