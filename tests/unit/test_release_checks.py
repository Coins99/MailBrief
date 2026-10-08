"""Release gates fail on missing service coverage and accidentally bundled private files."""

from pathlib import Path

import pytest

from scripts.check_coverage import SERVICES, check
from scripts.check_package import audit_distribution


def test_service_coverage_counts_branches_and_requires_every_service(tmp_path: Path) -> None:
    report = tmp_path / "coverage.xml"
    classes = [
        f'<class filename="services/{name}.py"><lines>'
        '<line hits="1" condition-coverage="100% (2/2)"/>'
        "</lines></class>"
        for name in SERVICES
    ]
    report.write_text("<coverage>" + "".join(classes) + "</coverage>", encoding="utf-8")
    assert check(report) == []
    report.write_text(
        "<coverage>" + "".join(classes).replace("100% (2/2)", "50% (1/2)", 1) + "</coverage>",
        encoding="utf-8",
    )
    # The first service in the list is the one made to fail.
    assert check(report) == [f"services/{SERVICES[0]}.py: 66.67% below 90%"]
    report.write_text("<coverage/>", encoding="utf-8")
    assert len(check(report)) == len(SERVICES)


def test_every_service_module_is_gated() -> None:
    services = Path(__file__).resolve().parents[2] / "src" / "mailbrief" / "services"
    modules = {path.stem for path in services.glob("*.py")} - {"__init__"}
    assert set(SERVICES) == modules
    assert len(SERVICES) == len(modules)
    assert {"analysis", "brief", "actions", "application"} <= set(SERVICES)


@pytest.mark.parametrize(
    "name",
    [
        "client_secret_example.json",
        "owner.sqlite3",
        "desktop.log",
        "token.json",
        ".env",
    ],
)
def test_distribution_audit_refuses_private_files(tmp_path: Path, name: str) -> None:
    (tmp_path / "MailBrief.exe").write_bytes(b"synthetic binary")
    audit_distribution(tmp_path)
    (tmp_path / name).write_bytes(b"synthetic private marker")
    with pytest.raises(ValueError, match="private or development"):
        audit_distribution(tmp_path)
