"""Follow-up proposals in the desktop: the brief's lines and links, the actions pane and its
Proposals dialog, and the window's apply and dismiss with Undo (ADR 0016)."""

import asyncio
import re
import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QCloseEvent, QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

from mailbrief.domain.actions import Action, ActionFilter, ActionProposal, ProposalState
from mailbrief.domain.analysis import DeadlinePrecision, FollowUpKind
from mailbrief.domain.briefs import BriefRunResult, BriefStatus
from mailbrief.domain.digests import (
    SECTION_TITLES,
    DailyDigest,
    DigestSection,
    DigestStatus,
)
from mailbrief.services.actions import ActionConflictError
from mailbrief.services.proposals import ProposalNotFoundError
from mailbrief.ui.actions_view import PROPOSALS, ActionsPanel, describe
from mailbrief.ui.digest_view import APPLY, DISMISS, DigestView
from mailbrief.ui.main_window import MainWindow
from mailbrief.ui.proposals_view import (
    NONE_PENDING,
    ProposalsDialog,
    effect_text,
    pending_proposals,
)
from tests.factories import make_action, make_digest_item, make_proposal
from tests.ui.brief_view import detail, shown_text
from tests.ui.test_workflow import FakeBackend

KEY = "reply-1"
TORONTO = ZoneInfo("America/Toronto")
UTC_ZONE = ZoneInfo("UTC")
NEW_DATETIME = {
    "kind": FollowUpKind.NEW_DEADLINE,
    "deadline_text": "Monday 5 PM",
    "deadline_precision": DeadlinePrecision.DATETIME,
    "deadline_date": date(2026, 10, 5),
    "deadline_at_utc": datetime(2026, 10, 5, 21, 0, tzinfo=UTC),
    "deadline_timezone": "America/Toronto",
}
NEW_DATE = {
    "kind": FollowUpKind.NEW_DEADLINE,
    "deadline_text": "next Monday",
    "deadline_precision": DeadlinePrecision.DATE,
    "deadline_date": date(2026, 10, 5),
    "deadline_timezone": "America/Toronto",
}
NEW_WORDS = {
    "kind": FollowUpKind.NEW_DEADLINE,
    "deadline_text": "when the board meets",
    "deadline_precision": DeadlinePrecision.UNRESOLVED,
}


def brief(zone: str = "America/Toronto") -> DailyDigest:
    return DailyDigest(
        account_id="owner@example.com",
        local_date=date(2026, 9, 4),
        timezone_name=zone,
        generated_at_utc=datetime(2026, 9, 4, 12, tzinfo=UTC),
        status=DigestStatus.COMPLETE,
        items=(
            make_digest_item(
                message_key=KEY,
                source_url="https://mail.google.com/mail/u/?authuser=owner%40example.com#all/a",
            ),
        ),
    )


def anchors(view: DigestView) -> list[str]:
    return re.findall(r'href="([^"]*)"', view.toHtml())


def shown(*proposals: ActionProposal, zone: str = "America/Toronto") -> tuple[DigestView, str]:
    view = DigestView()
    view.zone = UTC_ZONE
    view.show_digest(brief(zone), None, {KEY: proposals})
    return view, view.toPlainText()


# Effect names


@pytest.mark.parametrize(
    ("fields", "effect"),
    [
        # An exact time reads in the zone shown: 21:00 UTC is 17:00 in Toronto.
        (NEW_DATETIME, "Set the deadline to 2026-10-05 17:00"),
        (NEW_DATE, "Set the deadline to 2026-10-05"),
        (NEW_WORDS, "Set the deadline to “when the board meets”"),
        ({"kind": FollowUpKind.CANCELLED}, "Complete it (cancelled)"),
        ({"kind": FollowUpKind.DELIVERED}, "Complete it (delivered)"),
    ],
    ids=["datetime", "date", "words", "cancelled", "delivered"],
)
def test_a_proposal_is_named_by_its_effect(fields: dict[str, object], effect: str) -> None:
    proposal = make_proposal(**fields)

    assert effect_text(proposal, TORONTO) == effect
    if proposal.deadline_precision is DeadlinePrecision.DATETIME:
        assert effect_text(proposal, UTC_ZONE) == "Set the deadline to 2026-10-05 21:00"


# The brief


@pytest.mark.parametrize(
    ("fields", "effect"),
    [
        (NEW_DATETIME, "Set the deadline to 2026-10-05 17:00"),
        (NEW_DATE, "Set the deadline to 2026-10-05"),
        (NEW_WORDS, "Set the deadline to “when the board meets”"),
        ({"kind": FollowUpKind.CANCELLED}, "Complete it (cancelled)"),
        ({"kind": FollowUpKind.DELIVERED}, "Complete it (delivered)"),
    ],
    ids=["datetime", "date", "words", "cancelled", "delivered"],
)
def test_the_brief_shows_each_proposal_under_its_item_with_an_effect_named_link(
    qtbot: QtBot, fields: dict[str, object], effect: str
) -> None:
    view, text = shown(make_proposal(**fields))
    qtbot.addWidget(view)

    assert "Proposes for “Approve the proposal”: “No longer needed, thanks”" in text
    assert f"{effect} · Dismiss" in text
    # The proposal sits between the item and its footer links.
    assert text.index("Approve the proposal by Friday.") < text.index("Proposes for")
    assert text.index("Proposes for") < text.index("Draft a reply")
    assert all(link.startswith("mailbrief:") for link in anchors(view))


def test_an_exact_deadline_reads_in_the_brief_s_zone_not_the_owner_s(qtbot: QtBot) -> None:
    proposal = make_proposal(**NEW_DATETIME)
    view, text = shown(proposal, zone="Asia/Tokyo")  # 21:00 UTC is 06:00 the next day.
    qtbot.addWidget(view)

    assert "Set the deadline to 2026-10-06 06:00" in text


def test_proposal_links_emit_apply_or_dismiss_with_the_proposal_and_its_revision(
    qtbot: QtBot,
) -> None:
    first = make_proposal(id=3, action_revision=4)
    second = make_proposal(id=9, action_revision=7, kind=FollowUpKind.DELIVERED)
    view, _ = shown(first, second)
    qtbot.addWidget(view)
    requests: list[tuple[str, int, int]] = []
    view.proposal_requested.connect(lambda *request: requests.append(request))

    links = ["mailbrief:proposal/0", "mailbrief:proposal/1", "mailbrief:proposal/2"]
    for link in (*links, "mailbrief:proposal/3", "mailbrief:proposal/4", "mailbrief:proposal/x"):
        view.anchorClicked.emit(QUrl(link))

    assert requests == [
        ("apply", 3, 4),
        ("dismiss", 3, 4),
        ("apply", 9, 7),
        ("dismiss", 9, 7),
    ]
    view.show_digest(brief())  # Without proposals the old links do nothing.
    view.anchorClicked.emit(QUrl("mailbrief:proposal/0"))
    assert len(requests) == 4
    assert "Proposes for" not in view.toPlainText()


def test_only_pending_proposals_are_shown(qtbot: QtBot) -> None:
    applied = make_proposal(state=ProposalState.APPLIED)
    view, text = shown(applied, make_proposal(id=4, evidence="Quietly dropped"))
    qtbot.addWidget(view)

    assert text.count("Proposes for") == 1 and "Quietly dropped" in text
    assert [link for link in anchors(view) if link.startswith("mailbrief:proposal/")] == [
        "mailbrief:proposal/0",
        "mailbrief:proposal/1",
    ]


def test_everything_an_email_or_the_owner_wrote_is_escaped(qtbot: QtBot) -> None:
    hostile = '<a href="https://evil.example">x</a> & <b>bold</b>'
    proposal = make_proposal(
        action_title=hostile,
        evidence="Click <img src=x> & go",
        **{**NEW_WORDS, "deadline_text": hostile},
    )
    view, text = shown(proposal)
    qtbot.addWidget(view)

    assert f"Proposes for “{hostile}”: “Click <img src=x> & go”" in text
    assert f"Set the deadline to “{hostile}”" in text
    assert "evil.example" not in "".join(anchors(view))
    assert all(link.startswith("mailbrief:") for link in anchors(view))
    assert "<b>" not in view.toHtml().split("Proposes for")[1].split("</p>")[0]


def test_sections_use_one_shared_set_of_titles(qtbot: QtBot) -> None:
    sections = (
        DigestSection.ACTIONS,
        DigestSection.DEADLINES,
        DigestSection.DECISIONS,
        DigestSection.HIGHLIGHTS,
        DigestSection.FOLLOW_UPS,
    )
    digest = DailyDigest(
        account_id="owner@example.com",
        local_date=date(2026, 9, 4),
        timezone_name="UTC",
        generated_at_utc=datetime(2026, 9, 4, 12, tzinfo=UTC),
        status=DigestStatus.COMPLETE,
        items=tuple(
            make_digest_item(message_key=f"m{index}", position=index, section=section)
            for index, section in enumerate(sections)
        ),
    )
    view = DigestView()
    view.zone = UTC_ZONE
    qtbot.addWidget(view)

    view.show_digest(digest)

    headings = re.findall(r"<h3[^>]*>.*?</h3>", view.toHtml(), re.S)
    plain = [re.sub(r"<[^>]+>", "", heading).strip() for heading in headings]
    assert plain == [
        "Actions",  # The four existing titles are unchanged...
        "Deadlines",
        "Decisions",
        "Highlights",
        "Replies in threads you track",  # ...and the new one.
    ]
    assert SECTION_TITLES == {
        DigestSection.ACTIONS: "Actions",
        DigestSection.DEADLINES: "Deadlines",
        DigestSection.DECISIONS: "Decisions",
        DigestSection.HIGHLIGHTS: "Highlights",
        DigestSection.FOLLOW_UPS: "Replies in threads you track",
    }
    assert set(SECTION_TITLES) == set(DigestSection)


# The actions pane


NOW = datetime(2026, 9, 5, 15, tzinfo=UTC)
COMPLETED: dict[str, object] = {"status": "completed", "completed_at_utc": NOW}


def describe_action(action: object) -> str:
    return describe(action, today=date(2026, 9, 5), zone=UTC_ZONE, now=NOW)  # type: ignore[arg-type]


def test_an_action_line_counts_its_pending_proposals() -> None:
    assert "proposal" not in describe_action(make_action(title="Send the deck"))
    one = make_action(title="Send the deck", proposals=(make_proposal(),))
    two = make_action(title="Send the deck", proposals=(make_proposal(id=1), make_proposal(id=2)))

    assert describe_action(one).endswith(" · 1 proposal")
    assert describe_action(two).endswith(" · 2 proposals")
    assert describe_action(two).startswith("Send the deck — ")


def test_a_completed_action_shows_no_proposals() -> None:
    done = make_action(title="Send the deck", **COMPLETED, proposals=(make_proposal(),))

    assert pending_proposals(done) == ()
    assert "proposal" not in describe_action(done)


@pytest.fixture
def panel(qtbot: QtBot) -> ActionsPanel:
    result = ActionsPanel()
    qtbot.addWidget(result)
    return result


def show(panel: ActionsPanel, *actions: object) -> None:
    panel.show_actions(
        ActionFilter.OPEN,
        actions,  # type: ignore[arg-type]
        today=date(2026, 9, 5),
        zone=UTC_ZONE,
        now=NOW,
    )


def test_the_proposals_button_needs_an_action_with_pending_proposals(panel: ActionsPanel) -> None:
    assert not panel.proposals_button.isEnabled()  # Nothing selected.
    show(panel, make_action())
    assert not panel.proposals_button.isEnabled()  # No proposals.

    show(panel, make_action(proposals=(make_proposal(),)))
    assert panel.proposals_button.isEnabled()
    panel.set_busy(True)
    assert not panel.proposals_button.isEnabled()
    panel.set_busy(False)
    assert panel.proposals_button.isEnabled()

    show(panel, make_action(**COMPLETED, proposals=(make_proposal(),)))
    assert not panel.proposals_button.isEnabled()  # Only an open action can be updated.


def test_the_proposals_button_asks_for_the_selected_action(panel: ActionsPanel) -> None:
    action = make_action(proposals=(make_proposal(),))
    show(panel, action)
    requests: list[tuple[str, object]] = []
    panel.action_requested.connect(lambda kind, chosen: requests.append((kind, chosen)))

    panel.proposals_button.click()
    panel.set_busy(True)
    panel.proposals_button.click()  # Disabled: nothing more is asked.

    assert requests == [(PROPOSALS, action)]
    assert panel.action_with_id(action.public_id) == action
    assert panel.action_with_id("missing") is None


def mnemonic(text: str) -> str | None:
    """The letter after a single &, the way Qt marks a mnemonic (&& is a literal &). Qt
    itself ignores mnemonics on macOS, so this reads the marker directly."""
    found = re.search(r"(?<!&)&(?!&)(.)", text)
    return None if found is None else found.group(1).lower()


def test_the_panel_s_buttons_have_distinct_mnemonics(panel: ActionsPanel) -> None:
    mnemonics = [
        mnemonic(button.text())
        for button in (
            panel.edit_button,
            panel.complete_button,
            panel.delete_button,
            panel.source_button,
            panel.seen_button,
            panel.proposals_button,
            panel.draft_button,
        )
    ]

    assert all(mnemonics) and len(set(mnemonics)) == len(mnemonics)
    assert mnemonic(panel.proposals_button.text()) == "r"


# The dialog


@pytest.fixture
def dialog(qtbot: QtBot) -> Iterator[ProposalsDialog]:
    """The dialog on its own: the window's handlers would make it busy."""
    parent = QWidget()  # qtbot keeps widgets weakly, so the parent stays referenced here.
    qtbot.addWidget(parent)
    yield ProposalsDialog(parent)


def two_proposals() -> tuple[ActionProposal, ActionProposal]:
    return (
        make_proposal(id=3, sender_address="sam@example.com", **NEW_DATE),
        make_proposal(id=9, sender_address="kim@example.com", kind=FollowUpKind.DELIVERED),
    )


def test_the_dialog_lists_effect_quote_sender_and_local_date(dialog: ProposalsDialog) -> None:
    action = make_action(proposals=two_proposals())

    dialog.show_proposals(action, TORONTO)

    assert dialog.listing.count() == 2
    first = dialog.listing.item(0)
    assert first is not None
    assert first.text() == (
        "Set the deadline to 2026-10-05 · “No longer needed, thanks” · "
        "sam@example.com · 2026-09-03 09:00"  # 13:00 UTC is 09:00 in Toronto.
    )
    assert dialog.heading.text() == "Proposals for “Approve the proposal”"
    assert dialog.action_public_id == action.public_id
    proposal = dialog.proposal(9)
    assert proposal is not None and proposal.kind is FollowUpKind.DELIVERED
    assert dialog.proposal(404) is None


def test_the_apply_button_follows_the_selected_rows_effect(dialog: ProposalsDialog) -> None:
    dialog.show_proposals(make_action(proposals=two_proposals()), TORONTO)
    assert dialog.apply_button.text() == "&Apply: Set the deadline to 2026-10-05"
    assert dialog.apply_button.accessibleName() == "Apply: Set the deadline to 2026-10-05"

    dialog.listing.setCurrentRow(1)

    assert dialog.apply_button.text() == "&Apply: Complete it (delivered)"
    assert dialog.apply_button.accessibleName() == "Apply: Complete it (delivered)"


def test_text_from_an_email_is_plain_and_never_a_mnemonic(dialog: ProposalsDialog) -> None:
    words = make_proposal(**{**NEW_WORDS, "deadline_text": "Q&A <b>day</b>"})
    dialog.show_proposals(make_action(proposals=(words,)), TORONTO)

    # An & in the label would read as a mnemonic marker; it is doubled, and the accessible
    # name, which Qt doesn't parse, keeps the single one. The one mnemonic is the button's own.
    assert dialog.apply_button.text() == "&Apply: Set the deadline to “Q&&A <b>day</b>”"
    assert dialog.apply_button.accessibleName() == "Apply: Set the deadline to “Q&A <b>day</b>”"
    assert mnemonic(dialog.apply_button.text()) == "a"
    assert len(re.findall(r"(?<!&)&(?!&)", dialog.apply_button.text())) == 1
    item = dialog.listing.item(0)
    assert item is not None and "Q&A <b>day</b>" in item.text()
    assert dialog.heading.textFormat() == Qt.TextFormat.PlainText


def test_the_dialog_emits_apply_and_dismiss_for_the_selected_proposal(
    dialog: ProposalsDialog,
) -> None:
    dialog.show_proposals(make_action(proposals=two_proposals()), TORONTO)
    applied: list[int] = []
    dismissed: list[int] = []
    dialog.apply_requested.connect(applied.append)
    dialog.dismiss_requested.connect(dismissed.append)

    dialog.apply_button.click()
    dialog.listing.setCurrentRow(1)
    dialog.dismiss_button.click()
    dialog.apply_button.click()

    assert applied == [3, 9] and dismissed == [9]


def test_only_the_button_applies_never_return_or_a_double_click(
    dialog: ProposalsDialog,
) -> None:
    dialog.show_proposals(make_action(proposals=two_proposals()), TORONTO)
    asked: list[tuple[str, int]] = []
    dialog.apply_requested.connect(lambda proposal_id: asked.append(("apply", proposal_id)))
    dialog.dismiss_requested.connect(lambda proposal_id: asked.append(("dismiss", proposal_id)))
    dialog.show()
    dialog.listing.setFocus()

    # On the list: Return and Enter are consumed, and a double-click activates the row.
    QTest.keyClick(dialog.listing, Qt.Key.Key_Return)
    QTest.keyClick(dialog.listing, Qt.Key.Key_Enter)
    row = dialog.listing.visualItemRect(dialog.listing.item(0))
    QTest.mouseDClick(dialog.listing.viewport(), Qt.MouseButton.LeftButton, pos=row.center())
    # On the buttons: Return only clicks a button that is the dialog's default.
    for button in (dialog.apply_button, dialog.dismiss_button):
        button.setFocus()
        QTest.keyClick(button, Qt.Key.Key_Return)

    assert asked == []
    assert not dialog.apply_button.autoDefault() and not dialog.dismiss_button.autoDefault()

    # Anywhere else in the dialog, Return reaches the default button, which only closes it.
    dialog.heading.setFocus()
    QTest.keyClick(dialog, Qt.Key.Key_Return)
    assert asked == [] and not dialog.isVisible()

    # The buttons themselves still work.
    dialog.apply_button.click()
    dialog.dismiss_button.click()
    assert asked == [("apply", 3), ("dismiss", 3)]


def test_the_apply_button_has_its_own_mnemonic(dialog: ProposalsDialog) -> None:
    dialog.show_proposals(make_action(proposals=two_proposals()), TORONTO)
    buttons = (dialog.apply_button, dialog.dismiss_button, dialog.close_button)

    for row in (0, 1):
        dialog.listing.setCurrentRow(row)
        letters = [mnemonic(button.text()) for button in buttons]
        assert letters == ["a", "d", "c"]  # Whatever the row, and distinct from the others.


@pytest.mark.skipif(sys.platform == "darwin", reason="Qt ignores mnemonics on macOS")
def test_the_mnemonic_is_a_real_shortcut_where_qt_has_mnemonics(dialog: ProposalsDialog) -> None:
    dialog.show_proposals(make_action(proposals=two_proposals()), TORONTO)

    assert QKeySequence.mnemonic(dialog.apply_button.text()).toString() == "Alt+A"


def test_a_busy_dialog_acts_on_nothing(dialog: ProposalsDialog) -> None:
    dialog.show_proposals(make_action(proposals=two_proposals()), TORONTO)
    asked: list[int] = []
    dialog.apply_requested.connect(asked.append)
    dialog.dismiss_requested.connect(asked.append)

    dialog.set_busy(True)
    dialog.apply_button.click()
    dialog.dismiss_button.click()
    assert asked == [] and not dialog.apply_button.isEnabled()
    assert not dialog.dismiss_button.isEnabled() and dialog.close_button.isEnabled()

    dialog.set_busy(False)
    assert dialog.apply_button.isEnabled()


def test_refreshing_keeps_the_selection_and_an_empty_dialog_says_so(
    dialog: ProposalsDialog,
) -> None:
    action = make_action(proposals=two_proposals())
    dialog.show_proposals(action, TORONTO)
    dialog.listing.setCurrentRow(1)

    dialog.show_proposals(action, TORONTO)
    assert dialog.listing.currentRow() == 1  # The same proposal stays selected.

    dialog.show_proposals(make_action(proposals=(two_proposals()[0],)), TORONTO)
    assert dialog.listing.currentRow() == 0  # The selected one is gone.

    for gone in (make_action(), None):
        dialog.show_proposals(gone, TORONTO)
        assert dialog.listing.count() == 0 and NONE_PENDING in dialog.heading.text()
        assert not dialog.apply_button.isEnabled() and dialog.apply_button.text() == "&Apply"
    assert dialog.action_public_id is None


# The window

PROPOSAL = make_proposal(id=3, action_revision=4, **NEW_DATE)
ACTION_ID = "0c5e2c1d-6b8e-4f55-9d0e-2a7f3b9c1e44"


@pytest.fixture
def backend() -> FakeBackend:
    result = FakeBackend()
    result.saved = brief("UTC")
    result.proposals = {KEY: (PROPOSAL,)}
    return result


@pytest.fixture
def window(qtbot: QtBot, backend: FakeBackend) -> MainWindow:
    result = MainWindow(backend)
    result.zone = UTC_ZONE
    result.now = lambda: NOW
    qtbot.addWidget(result)
    return result


async def finish(window: MainWindow) -> None:
    assert window.task is not None
    await window.task


async def test_every_brief_is_shown_with_its_proposals(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()

    assert backend.proposal_calls == [backend.saved]
    assert "Proposes for “Approve the proposal”" in shown_text(window)


async def test_a_brief_whose_proposals_fail_to_load_is_shown_without_them(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.proposals_fail = RuntimeError("private detail")
    await window.initialize()

    assert "Approval needed by Friday" in shown_text(window)
    assert "Proposes for" not in shown_text(window)
    assert "private detail" not in window.status.text()


async def test_applying_from_the_brief_runs_one_operation_then_undo_reverses_it(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    loads, lists = backend.loads, backend.list_calls

    detail(window).proposal_requested.emit(APPLY, 3, 4)
    await finish(window)

    assert backend.action_calls == [("apply_proposal", 3, 4)]  # At the revision it was shown at.
    assert window.status.text() == "Applied to: Send the deck."
    assert (backend.loads, backend.list_calls) == (loads + 1, lists + 3)  # Brief and actions.
    assert window.undo_button.text() == "&Undo apply"

    window.undo_button.click()
    await finish(window)

    # Undo names the proposal and the revision the apply made (the fake adds one).
    assert backend.action_calls[-1] == ("undo_apply_proposal", 3, 5)
    assert window.status.text() == "Undone."
    assert window.undo_button.isHidden()


async def test_dismissing_from_the_brief_then_undo_restores_it(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()

    detail(window).proposal_requested.emit(DISMISS, 3, 4)
    await finish(window)

    assert backend.action_calls == [("dismiss_proposal", 3)]
    assert window.status.text() == "Proposal dismissed. It won't be proposed again."
    assert window.undo_button.text() == "&Undo dismiss"

    window.undo_button.click()
    await finish(window)

    assert backend.action_calls[-1] == ("restore_proposal", 3)
    assert window.status.text() == "Undone."


@pytest.mark.parametrize("link", [APPLY, DISMISS])
async def test_a_conflict_shows_its_static_message_and_refreshes(
    window: MainWindow, backend: FakeBackend, link: str
) -> None:
    await window.initialize()
    message = "The action changed since it was loaded; reload it and try again."
    backend.action_fail = ActionConflictError(message)
    loads, lists = backend.loads, backend.list_calls

    detail(window).proposal_requested.emit(link, 3, 4)
    await finish(window)

    assert window.status.text() == message
    assert (backend.loads, backend.list_calls) == (loads + 1, lists + 3)
    assert window.undo_button.isHidden()  # Nothing changed, so nothing to undo.


@pytest.mark.parametrize("link", [APPLY, DISMISS])
async def test_a_proposal_that_is_gone_reloads(
    window: MainWindow, backend: FakeBackend, link: str
) -> None:
    await window.initialize()
    backend.action_fail = ProposalNotFoundError()

    detail(window).proposal_requested.emit(link, 3, 4)
    await finish(window)

    assert window.status.text() == "That changed or is no longer available; the view was reloaded."
    assert window.undo_button.isHidden()


async def test_a_refused_undo_explains_itself(window: MainWindow, backend: FakeBackend) -> None:
    await window.initialize()
    detail(window).proposal_requested.emit(APPLY, 3, 4)
    await finish(window)
    backend.action_fail = ActionConflictError("Only an unchanged update can be undone.")

    window.undo_button.click()
    await finish(window)

    assert window.status.text() == "Can't undo: it has changed since then."


async def test_a_click_while_busy_is_refused_with_a_reason(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    release = asyncio.Event()

    async def hold() -> None:
        await release.wait()

    window.start(hold)  # Something else is running.
    await asyncio.sleep(0)

    detail(window).proposal_requested.emit(APPLY, 3, 4)

    assert window.status.text() == "MailBrief is busy; try again in a moment."
    assert backend.action_calls == []
    release.set()
    await finish(window)


# The Proposals dialog


def tracked_action(revision: int = 2) -> object:
    return make_action(
        title="Send the deck",
        revision=revision,
        proposals=(
            make_proposal(id=3, action_revision=revision, **NEW_DATE),
            make_proposal(id=9, action_revision=revision, kind=FollowUpKind.DELIVERED),
        ),
    )


@pytest.fixture
async def opened(window: MainWindow, backend: FakeBackend) -> MainWindow:
    """The window with one action that has two proposals, and its dialog open."""
    backend.actions = {ActionFilter.OPEN: (tracked_action(),)}  # type: ignore[dict-item]
    await window.initialize()
    assert "2 proposals" in window.actions_panel.lists[ActionFilter.OPEN].item(0).text()
    window.actions_panel.proposals_button.click()
    return window


async def test_the_actions_pane_opens_the_dialog_for_the_selected_action(
    opened: MainWindow,
) -> None:
    dialog = opened.proposals_dialog

    assert dialog.isVisible()
    assert dialog.heading.text() == "Proposals for “Send the deck”"
    assert dialog.listing.count() == 2
    assert dialog.apply_button.text() == "&Apply: Set the deadline to 2026-10-05"


async def test_applying_in_the_dialog_uses_the_action_revision_and_refreshes_it(
    opened: MainWindow, backend: FakeBackend
) -> None:
    dialog = opened.proposals_dialog
    dialog.listing.setCurrentRow(1)
    real = backend.apply_proposal

    async def apply_then_change(proposal_id: int, revision: int) -> Action:
        result = await real(proposal_id, revision)
        # The action as it is after: completed proposals are no longer pending.
        backend.actions = {
            ActionFilter.OPEN: (
                make_action(
                    title="Send the deck",
                    revision=3,
                    proposals=(make_proposal(id=3, action_revision=3, **NEW_DATE),),
                ),
            )
        }
        return result

    backend.apply_proposal = apply_then_change  # type: ignore[method-assign]

    dialog.apply_button.click()
    await finish(opened)

    assert backend.action_calls == [("apply_proposal", 9, 2)]
    assert opened.status.text() == "Applied to: Send the deck."
    assert opened.undo_button.text() == "&Undo apply"
    # The open dialog followed the refreshed action: one proposal left, at the new revision.
    assert dialog.isVisible() and dialog.listing.count() == 1
    proposal = dialog.proposal(3)
    assert proposal is not None and proposal.action_revision == 3
    assert dialog.proposal(9) is None


async def test_dismissing_in_the_dialog_then_undo(opened: MainWindow, backend: FakeBackend) -> None:
    opened.proposals_dialog.dismiss_button.click()
    await finish(opened)

    assert backend.action_calls == [("dismiss_proposal", 3)]
    assert opened.undo_button.text() == "&Undo dismiss"
    opened.undo_button.click()
    await finish(opened)
    assert backend.action_calls[-1] == ("restore_proposal", 3)


async def test_the_dialog_is_busy_while_an_operation_runs(opened: MainWindow) -> None:
    dialog = opened.proposals_dialog
    release = asyncio.Event()

    async def hold() -> None:
        await release.wait()

    opened.start(hold)
    await asyncio.sleep(0)

    assert not dialog.apply_button.isEnabled() and not dialog.dismiss_button.isEnabled()
    release.set()
    await finish(opened)
    assert dialog.apply_button.isEnabled() and dialog.dismiss_button.isEnabled()


async def test_a_closed_dialog_is_not_refreshed_and_closing_the_window_closes_it(
    opened: MainWindow, backend: FakeBackend
) -> None:
    dialog = opened.proposals_dialog
    dialog.close_button.click()
    assert not dialog.isVisible()
    backend.actions = {ActionFilter.OPEN: ()}

    await opened._refresh_views()
    assert dialog.listing.count() == 2  # Not refreshed: nobody is looking at it.

    opened.actions_panel.proposals_button.click()  # Reopened from a pane with no action.
    assert not dialog.isVisible()
    dialog.show_proposals(tracked_action(), UTC_ZONE)  # type: ignore[arg-type]
    dialog.open()
    opened.closeEvent(QCloseEvent())
    assert not dialog.isVisible()


async def test_a_vanished_action_empties_an_open_dialog(
    opened: MainWindow, backend: FakeBackend
) -> None:
    dialog = opened.proposals_dialog
    backend.actions = {ActionFilter.OPEN: ()}

    await opened._refresh_views()

    assert dialog.isVisible() and dialog.listing.count() == 0
    assert NONE_PENDING in dialog.heading.text()


# The status line after a run


async def run_brief(window: MainWindow) -> None:
    window.start(window._generate)
    for _ in range(3):
        await asyncio.sleep(0)
    window.review_button.click()
    for _ in range(3):
        await asyncio.sleep(0)
    window.approve_button.click()
    await finish(window)


@pytest.mark.parametrize(
    ("created", "sentence"),
    [
        (0, ""),
        (1, " Proposed 1 update to your actions."),
        (3, " Proposed 3 updates to your actions."),
    ],
)
async def test_a_run_says_how_many_updates_it_proposed(
    window: MainWindow, backend: FakeBackend, created: int, sentence: str
) -> None:
    backend.proposals_created = created
    await window.initialize()

    await run_brief(window)

    assert window.status.text() == "Brief saved (complete)." + sentence


async def test_the_proposal_sentence_follows_the_thread_sentence(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.proposals_created = 2
    backend.sync = backend.sync.model_copy(update={"threads_tracked": 2, "threads_checked": 2})
    await window.initialize()

    await run_brief(window)

    assert window.status.text() == (
        "Brief saved (complete). Checked 2 tracked threads. Proposed 2 updates to your actions."
    )


async def test_a_cancelled_run_proposes_nothing(window: MainWindow, backend: FakeBackend) -> None:
    await window.initialize()

    async def cancelled(*_args: object, **_kwargs: object) -> BriefRunResult:
        return BriefRunResult(status=BriefStatus.CANCELLED, sync=backend.sync)

    backend.generate = cancelled  # type: ignore[method-assign]
    window.start(window._generate)
    await finish(window)

    assert "Proposed" not in window.status.text()
