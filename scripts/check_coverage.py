"""Enforce the service coverage contract in addition to pytest's overall gate."""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

SERVICES = (
    "sync",
    "ranking",
    "bodies",
    "digest",
    "drafts",
    "drafting",
    "preferences",
    "history",
    "threads",
    "proposals",
    "consent",
    "data",
)


def check(report: Path) -> list[str]:
    classes = tuple(ET.parse(report).iter("class"))
    failures = []
    for name in SERVICES:
        suffix = f"services/{name}.py"
        matches = [
            item for item in classes if item.attrib["filename"].replace("\\", "/").endswith(suffix)
        ]
        if len(matches) != 1:
            failures.append(f"{suffix}: coverage missing or ambiguous")
            continue
        covered = total = 0
        for line in matches[0].iter("line"):
            total += 1
            covered += int(int(line.attrib["hits"]) > 0)
            condition = line.attrib.get("condition-coverage", "")
            if condition:
                match = re.fullmatch(r"\d+% \((\d+)/(\d+)\)", condition)
                if match is None:
                    raise ValueError("Unexpected coverage branch format.")
                covered += int(match[1])
                total += int(match[2])
        if total == 0 or covered / total < 0.9:
            failures.append(f"{suffix}: {100 * covered / max(total, 1):.2f}% below 90%")
    return failures


def main() -> int:
    failures = check(Path(__file__).resolve().parents[1] / "coverage.xml")
    for failure in failures:
        print(failure)
    if not failures:
        print("Required services meet 90% combined statement/branch coverage.")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
