"""Build the desktop package on the destination OS using the locked environment."""

import argparse
import plistlib
import subprocess
import sys
from pathlib import Path

from mailbrief import __version__


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--console", action="store_true", help="Build with a troubleshooting console."
    )
    options = parser.parse_args()
    if sys.platform not in {"win32", "darwin"}:
        raise SystemExit("Build MailBrief on Windows or macOS.")
    root = Path(__file__).resolve().parents[1]
    output = root / "out"
    # PyInstaller replaces these generated targets; reject redirected output locations.
    for target in (
        output / "dist" / "MailBrief",
        output / "dist" / "MailBrief.app",
        output / "build",
        output / "spec",
    ):
        if not target.resolve().is_relative_to(root / "out"):
            raise SystemExit("Build output must stay inside the repository's out directory.")
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--console" if options.console else "--windowed",
        "--name",
        "MailBrief",
        "--noupx",
        "--disable-windowed-traceback",
        "--paths",
        str(root / "src"),
        "--distpath",
        str(output / "dist"),
        "--workpath",
        str(output / "build"),
        "--specpath",
        str(output / "spec"),
        "--add-data",
        f"{root / 'migrations' / 'env.py'}:migrations",
        "--add-data",
        f"{root / 'src' / 'mailbrief' / 'ui' / 'theme' / 'fonts'}:mailbrief/ui/theme/fonts",
        "--add-data",
        f"{root / 'src' / 'mailbrief' / 'ui' / 'theme' / 'icons'}:mailbrief/ui/theme/icons",
        "--collect-data",
        "tzdata",
        "--collect-submodules",
        "alembic.ddl",
        "--hidden-import",
        "sqlalchemy.dialects.sqlite.aiosqlite",
        "--hidden-import",
        "aiosqlite",
        "--hidden-import",
        "logging.config",
        "--hidden-import",
        "PySide6.QtSvg",
        "--exclude-module",
        "mypy",
        "--exclude-module",
        "pytest",
        "--exclude-module",
        "_pytest",
        "--exclude-module",
        "setuptools",
        "--hidden-import",
        "keyring.backends.Windows" if sys.platform == "win32" else "keyring.backends.macOS",
    ]
    for revision in sorted((root / "migrations" / "versions").glob("*.py")):
        command.extend(["--add-data", f"{revision}:migrations/versions"])
    if sys.platform == "darwin":
        command.extend(["--osx-bundle-identifier", "com.mailbrief.desktop"])
    command.append(str(root / "scripts" / "desktop_entry.py"))
    result = subprocess.call(command, cwd=root)
    if result == 0 and sys.platform == "darwin":
        bundle = output / "dist" / "MailBrief.app"
        plist = bundle / "Contents" / "Info.plist"
        with plist.open("rb") as stream:
            metadata = plistlib.load(stream)
        metadata.update(
            CFBundleShortVersionString=__version__,
            CFBundleVersion=__version__,
            LSMinimumSystemVersion="15.0",
        )
        with plist.open("wb") as stream:
            plistlib.dump(metadata, stream)
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(bundle)], check=True)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
