"""Shortlist review: excluded senders can't be selected, and the owner's limit applies."""

import asyncio
from collections.abc import AsyncIterator

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from pytestqt.qtbot import QtBot

from mailbrief.domain.messages import EmailContact, RankedMessage
from mailbrief.ui.main_window import MainWindow
from tests.factories import make_message
from tests.ui.test_workflow import FakeBackend

CANDIDATES = tuple(
    RankedMessage(
        message=make_message(
            provider_message_id=f"m{index}",
            subject=f"Subject {index}",
            sender=EmailContact(address=f"sender{index}@example.com"),
        ),
        score=10,
    )
    for index in range(4)
)


@pytest.fixture
async def window(qtbot: QtBot) -> AsyncIterator[MainWindow]:
    result = MainWindow(FakeBackend())
    qtbot.addWidget(result)
    result.show()
    yield result


async def start_review(
    window: MainWindow,
    blocked: frozenset[str],
    limit: int,
    outside: frozenset[str] = frozenset(),
) -> "asyncio.Task[tuple[str, ...] | None]":
    task = asyncio.create_task(
        window.review(CANDIDATES, ("m1",), blocked_ids=blocked, outside_ids=outside, limit=limit)
    )
    await asyncio.sleep(0)
    return task


async def stop(task: "asyncio.Task[tuple[str, ...] | None]") -> None:
    if not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_blocked_rows_are_listed_but_never_checkable(window: MainWindow) -> None:
    task = await start_review(window, frozenset({"m0", "m2"}), 10)
    try:
        blocked = window.shortlist.item(0)
        assert blocked is not None
        assert blocked.text() == "sender0@example.com — Subject 0 — excluded in Settings"
        assert "Settings > Preferences" in blocked.toolTip()
        assert not blocked.flags() & Qt.ItemFlag.ItemIsUserCheckable
        assert blocked.checkState() == Qt.CheckState.Unchecked

        window.shortlist.setCurrentRow(0)
        QTest.keyClick(window.shortlist, Qt.Key.Key_Space)
        assert blocked.checkState() == Qt.CheckState.Unchecked

        # Even a check state forced onto the row is ignored.
        blocked.setCheckState(Qt.CheckState.Checked)
        window.review_button.click()
        assert await task == ("m1",)
    finally:
        await stop(task)


async def test_space_still_checks_an_allowed_row(window: MainWindow) -> None:
    task = await start_review(window, frozenset({"m0"}), 10)
    try:
        window.shortlist.setCurrentRow(3)
        QTest.keyClick(window.shortlist, Qt.Key.Key_Space)
        window.review_button.click()
        assert await task == ("m1", "m3")
    finally:
        await stop(task)


async def test_the_owner_s_limit_is_enforced_with_its_message(window: MainWindow) -> None:
    task = await start_review(window, frozenset(), 2)
    try:
        assert "up to 2 messages" in window.review_hint.text()
        for index in (0, 1, 2):
            item = window.shortlist.item(index)
            assert item is not None
            item.setCheckState(Qt.CheckState.Checked)
        assert not window.review_button.isEnabled()
        assert window.status.text() == "Choose at most 2 messages before continuing."
        window._accept_review()
        await asyncio.sleep(0)
        assert not task.done()

        item = window.shortlist.item(2)
        assert item is not None
        item.setCheckState(Qt.CheckState.Unchecked)
        assert window.review_button.isEnabled()
        window.review_button.click()
        assert await task == ("m0", "m1")
    finally:
        await stop(task)


async def test_a_limit_of_one_reads_in_the_singular(window: MainWindow) -> None:
    task = await start_review(window, frozenset(), 1)
    try:
        assert "up to 1 message." in window.review_hint.text()
        item = window.shortlist.item(0)
        assert item is not None
        item.setCheckState(Qt.CheckState.Checked)
        assert window.status.text() == "Choose at most 1 message before continuing."
    finally:
        await stop(task)


OUTSIDE = "reply in a tracked thread, not in today's Inbox"


async def test_outside_replies_are_labelled_and_otherwise_like_any_other_row(
    window: MainWindow,
) -> None:
    task = await start_review(window, frozenset(), 10, outside=frozenset({"m2"}))
    try:
        labels = [item.text() for item in (window.shortlist.item(row) for row in range(4)) if item]
        assert labels == [
            "sender0@example.com — Subject 0",
            "sender1@example.com — Subject 1",
            f"sender2@example.com — Subject 2 — {OUTSIDE}",
            "sender3@example.com — Subject 3",
        ]
        outside = window.shortlist.item(2)
        assert outside is not None
        # Checkable and not checked until chosen, like the others that aren't suggested.
        assert outside.flags() & Qt.ItemFlag.ItemIsUserCheckable
        assert outside.checkState() == Qt.CheckState.Unchecked
        outside.setCheckState(Qt.CheckState.Checked)
        window.review_button.click()
        assert await task == ("m1", "m2")
    finally:
        await stop(task)


async def test_a_suggested_outside_reply_is_checked_by_default(window: MainWindow) -> None:
    task = asyncio.create_task(
        window.review(
            CANDIDATES,
            ("m2",),
            blocked_ids=frozenset(),
            outside_ids=frozenset({"m2"}),
            limit=10,
        )
    )
    await asyncio.sleep(0)
    try:
        outside = window.shortlist.item(2)
        assert outside is not None and outside.checkState() == Qt.CheckState.Checked
        assert outside.text().endswith(OUTSIDE)
        assert window.review_button.text() == "Co&ntinue with 1 selected message"
    finally:
        await stop(task)
