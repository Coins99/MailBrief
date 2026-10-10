"""Offline browsing and keyboard-driven shortlist inclusion."""

import asyncio
import re
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
import time_machine
from PySide6.QtCore import QDate, Qt
from PySide6.QtTest import QTest
from pytestqt.qtbot import QtBot

from mailbrief.domain.cached_mail import CachedAccount, CachedMailPage
from mailbrief.domain.messages import RankedMessage
from mailbrief.ui.main_window import MainWindow
from tests.factories import make_message
from tests.ui.test_workflow import FakeBackend, finish


async def test_keyboard_review_can_select_outside_suggestions_and_enforces_limit(
    qtbot: QtBot,
) -> None:
    window = MainWindow(FakeBackend())
    qtbot.addWidget(window)
    window.show()
    candidates = tuple(
        RankedMessage(
            message=make_message(provider_message_id=f"m{index}"),
            score=10,
        )
        for index in range(12)
    )
    task = asyncio.create_task(
        window.review(
            candidates,
            ("m0", "m1", "m2"),
            blocked_ids=frozenset(),
            outside_ids=frozenset(),
            declined_ids=frozenset(),
            limit=10,
        )
    )
    await asyncio.sleep(0)
    try:
        assert window.shortlist.count() == 12
        window.shortlist.setCurrentRow(11)
        QTest.keyClick(window.shortlist, Qt.Key.Key_Space)
        last = window.shortlist.item(11)
        assert last is not None and last.checkState() == Qt.CheckState.Checked
        for index in range(12):
            item = window.shortlist.item(index)
            assert item is not None
            item.setCheckState(Qt.CheckState.Checked)
        assert not window.review_button.isEnabled()
        window._accept_review()
        assert not task.done()
        for index in range(11):
            item = window.shortlist.item(index)
            assert item is not None
            item.setCheckState(Qt.CheckState.Unchecked)
        assert window.review_button.isEnabled()
        window.review_button.click()
        assert await task == ("m11",)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_offline_browsing_filters_pages_and_renders_preview_as_text(
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[int, date, int]] = []

    class CachedBackend(FakeBackend):
        async def cached_accounts(self) -> tuple[CachedAccount, ...]:
            return (
                CachedAccount(account_id=1, email_address="one@example.com"),
                CachedAccount(account_id=2, email_address="two@example.com"),
            )

        async def cached_messages(
            self, account_id: int, day: date, offset: int = 0
        ) -> CachedMailPage:
            requests.append((account_id, day, offset))
            return CachedMailPage(
                account=(await self.cached_accounts())[account_id - 1],
                local_date=day,
                timezone_name="UTC",
                offset=offset,
                has_more=offset == 0,
                messages=(make_message(body_preview='<img src="https://evil.example">'),),
            )

    backend = CachedBackend()
    window = MainWindow(backend)
    qtbot.addWidget(window)
    window.start(window.initialize)
    await finish(window)
    forbidden = AsyncMock(side_effect=AssertionError("No Gmail connection during offline browsing"))
    monkeypatch.setattr(backend, "connect", forbidden)
    window.cached_button.click()
    await finish(window)
    viewer = window.cached_dialog
    assert viewer.isVisible()
    assert '<img src="https://evil.example">' in viewer.details.toPlainText()
    assert "may be stale" in viewer.status.text()
    # Times read as everywhere else in the window, not as ISO.
    assert re.search(
        r"\nReceived: [A-Z][a-z]{2} [A-Z][a-z]{2} \d+(, \d{4})?, \d\d:\d\d\n",
        viewer.details.toPlainText(),
    )
    assert re.match(r"[A-Z][a-z]{2} [A-Z][a-z]{2} \d+(, \d{4})? \(UTC\) · ", viewer.status.text())
    assert not viewer.source.isEnabled()  # The factory's Outlook link cannot open from Gmail UI.
    viewer.next.click()
    await finish(window)
    assert requests[-1][2] == 100
    assert viewer.previous.isEnabled() and not viewer.next.isEnabled()
    viewer.accounts.setCurrentIndex(1)
    await finish(window)
    assert requests[-1][0] == 2 and requests[-1][2] == 0
    viewer.day.setDate(QDate(2026, 8, 31))
    await finish(window)
    assert requests[-1] == (2, date(2026, 8, 31), 0)
    forbidden.assert_not_called()


class OneAccountBackend(FakeBackend):
    """One cached account; records the pages asked for."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[tuple[int, date, int]] = []

    async def cached_accounts(self) -> tuple[CachedAccount, ...]:
        return (CachedAccount(account_id=1, email_address="one@example.com"),)

    async def cached_messages(self, account_id: int, day: date, offset: int = 0) -> CachedMailPage:
        self.requests.append((account_id, day, offset))
        return CachedMailPage(
            account=(await self.cached_accounts())[0],
            local_date=day,
            timezone_name="Pacific/Kiritimati",
            offset=offset,
            has_more=False,
            messages=(),
        )


async def test_browse_saved_mail_opens_on_today_in_your_time_zone(qtbot: QtBot) -> None:
    backend = OneAccountBackend()
    window = MainWindow(backend)
    qtbot.addWidget(window)
    window.start(window.initialize)
    await finish(window)
    # Kiritimati is UTC+14: at noon UTC on the 30th it is already 1 October there, a date no
    # computer west of it shares.
    window.cached_dialog.zone = ZoneInfo("Pacific/Kiritimati")
    viewer = window.cached_dialog
    assert viewer.day.accessibleName() == "Received date in your time zone"

    with time_machine.travel(datetime(2026, 9, 30, 12, tzinfo=UTC), tick=False):
        window.cached_button.click()
        await finish(window)

    assert viewer.day.date() == QDate(2026, 10, 1)
    assert backend.requests == [(1, date(2026, 10, 1), 0)]

    # Picking another day, closing and opening again starts from today once more.
    viewer.day.setDate(QDate(2026, 9, 20))
    await finish(window)
    viewer.reject()
    viewer.today = lambda: date(2026, 10, 2)  # An injected clock, a day later.
    window.cached_button.click()
    await finish(window)

    assert viewer.day.date() == QDate(2026, 10, 2)
    assert backend.requests[-1] == (1, date(2026, 10, 2), 0)
