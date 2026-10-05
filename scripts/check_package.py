"""Run a frozen package away from the checkout, using only disposable test data."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def audit_distribution(directory: Path) -> None:
    """Only inspect generated artifacts, never developer credentials or real profiles."""
    for path in directory.rglob("*"):
        name = path.name.lower()
        if (
            name in {".env", ".git", ".venv", "desktop-settings.json"}
            or name.startswith("client_secret")
            or name.endswith((".sqlite", ".sqlite3", ".db", ".log"))
            or name in {"credentials.json", "token.json", "token-cache.json"}
        ):
            raise ValueError("Distribution contains private or development artifacts.")


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    if sys.platform == "win32":
        executable = root / "out" / "dist" / "MailBrief" / "MailBrief.exe"
    elif sys.platform == "darwin":
        executable = root / "out" / "dist" / "MailBrief.app" / "Contents" / "MacOS" / "MailBrief"
    else:
        raise SystemExit("Package checks require Windows or macOS.")
    audit_distribution(executable.parent if sys.platform == "win32" else executable.parents[2])
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("MAILBRIEF_") and key not in {"PYTHONPATH", "PYTHONHOME"}
    }
    environment["QT_QPA_PLATFORM"] = "offscreen"
    with tempfile.TemporaryDirectory(prefix="package-check-", dir=root / "out") as temporary:
        directory = Path(temporary)
        report = directory / "smoke-result.json"
        for scale in ("1", "2"):
            environment["QT_SCALE_FACTOR"] = scale
            report.unlink(missing_ok=True)
            completed = subprocess.run(
                [str(executable), "--smoke-test-dir", str(directory)],
                cwd=directory,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=90,
                creationflags=0x08000000 if sys.platform == "win32" else 0,
            )
            if completed.returncode:
                # Smoke mode uses synthetic data only; console builds expose missing imports.
                print(completed.stderr)
                if report.is_file():
                    print(report.read_text(encoding="utf-8"))
                raise SystemExit("Package smoke process failed.")
            result = json.loads(report.read_text(encoding="utf-8"))
            if result != {"ok": True, "platform": sys.platform, "launches": 2}:
                raise SystemExit("Package smoke check returned an unexpected result.")
    print("Package smoke checks passed in two separate processes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
