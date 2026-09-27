"""Build the desktop package on the destination OS using the locked environment."""

import argparse
import subprocess
import sys
from pathlib import Path


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
        "--exclude-module",
        "mypy",
        "--exclude-module",
        "pytest",
        "--hidden-import",
        "keyring.backends.Windows" if sys.platform == "win32" else "keyring.backends.macOS",
    ]
    for revision in sorted((root / "migrations" / "versions").glob("*.py")):
        command.extend(["--add-data", f"{revision}:migrations/versions"])
    if sys.platform == "darwin":
        command.extend(["--osx-bundle-identifier", "com.mailbrief.desktop"])
    command.append(str(root / "scripts" / "desktop_entry.py"))
    return subprocess.call(command, cwd=root)


if __name__ == "__main__":
    raise SystemExit(main())
