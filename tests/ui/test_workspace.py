"""The three-pane workspace renders in both themes at the mockup and desktop sizes.

With MAILBRIEF_UI_SHOTS set to a folder, each render is saved there as
``{size}-{mode}-{dpr}x.png`` for the screenshot review loop (.claude/skills/mailbrief-ui).
"""

import os
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QLabel
from pytestqt.qtbot import QtBot

from mailbrief.domain.digests import DailyDigest, DigestStatus
from mailbrief.ui.theme import ThemeMode, apply_theme
from mailbrief.ui.workspace import EMPTY_BRIEF, ThreePaneWorkspace
from tests.ui.workspace_fixtures import mockup_digest

SIZES = {"mockup": (680, 520), "desktop": (1100, 720)}


def build(qtbot: QtBot) -> ThreePaneWorkspace:
    workspace = ThreePaneWorkspace()
    qtbot.addWidget(workspace)
    brief = mockup_digest()
    workspace.show_digest(brief.digest, links=brief.links, proposals=brief.proposals)
    workspace.header.set_status("Checked Gmail at 09:14")
    workspace.sidebar.set_counts(5, 2, 3)
    workspace.sidebar.set_note("Gmail connected. AI asks before sending.")
    return workspace


def show(qtbot: QtBot, workspace: ThreePaneWorkspace) -> None:
    workspace.show()
    try:
        qtbot.waitExposed(workspace, timeout=2000)
    except Exception:  # Some offscreen platforms never report exposure.
        QApplication.processEvents()
    QApplication.processEvents()


@pytest.mark.parametrize("size", list(SIZES))
@pytest.mark.parametrize("mode", list(ThemeMode))
def test_workspace_renders(
    qtbot: QtBot, qapp: QApplication, themed: None, mode: ThemeMode, size: str
) -> None:
    apply_theme(qapp, mode)
    workspace = build(qtbot)
    workspace.resize(*SIZES[size])
    show(qtbot, workspace)
    image = workspace.grab()
    assert not image.isNull()
    folder = os.environ.get("MAILBRIEF_UI_SHOTS")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        dpr = image.devicePixelRatio()
        assert image.save(str(Path(folder) / f"{size}-{mode.value}-{dpr:g}x.png"))


def test_sidebar_requests_pages_and_settings(qtbot: QtBot) -> None:
    workspace = build(qtbot)
    sidebar = workspace.sidebar
    pages = QSignalSpy(sidebar.page_requested)
    settings = QSignalSpy(sidebar.settings_requested)
    sidebar.set_current("drafts")
    assert pages.count() == 0  # Showing a page doesn't request it.
    sidebar.nav.setCurrentIndex(sidebar.pages.index(1, 0))
    assert pages.at(0) == ["actions"]
    sidebar.nav.setFocus()
    QTest.keyClick(sidebar.nav, Qt.Key.Key_Return)
    assert pages.at(1) == ["actions"]
    sidebar.settings.click()
    assert settings.count() == 1
    assert sidebar.count_text("actions") == "5"
    sidebar.set_counts(None, 2, 3)
    assert sidebar.count_text("actions") is None
    assert sidebar.note.text() == "Gmail connected. AI asks before sending."


def test_selecting_a_row_updates_the_detail(qtbot: QtBot) -> None:
    workspace = build(qtbot)
    assert workspace.detail.title is not None
    assert workspace.detail.title.text() == "Q3 budget: approval needed by Friday"
    workspace.brief_list.setCurrentIndex(workspace.brief_list.model().index(4, 0))
    assert workspace.detail.title.text() == "Invoice 2041"
    assert workspace.coverage.text().startswith("Covers messages received on 2026-10-06")
    assert workspace.header.status.text() == "Checked Gmail at 09:14"


def test_empty_brief_says_so(qtbot: QtBot) -> None:
    workspace = ThreePaneWorkspace()
    qtbot.addWidget(workspace)
    empty = DailyDigest.model_validate(
        {**mockup_digest().digest.model_dump(), "items": (), "status": DigestStatus.EMPTY}
    )
    workspace.show_digest(empty)
    assert workspace.brief_list.model().rowCount() == 0
    assert workspace.detail.title is None
    texts = [label.text() for label in workspace.detail.findChildren(QLabel)]
    assert EMPTY_BRIEF in texts
