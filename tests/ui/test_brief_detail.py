"""The detail pane: requests carry the right ids, Gmail links only, and hostile text stays
literal plain text."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QLabel, QPushButton, QWidget
from pytestqt.qtbot import QtBot

from mailbrief.domain.actions import ProposalState, SuggestionState, SuggestionView
from mailbrief.domain.analysis import DeadlinePrecision
from mailbrief.domain.messages import EmailContact
from mailbrief.ui.brief_detail import BriefDetailPane, item_deadline_text, suggestion_meta
from mailbrief.ui.digest_view import ACCEPT, APPLY, DISMISS
from tests.factories import make_digest_item, make_proposal, make_suggestion
from tests.ui.workspace_fixtures import ACCOUNT, DECK_ACTION, FINANCE_ACTION, ZONE, mockup_digest

TORONTO = ZoneInfo(ZONE)
HOSTILE = '<b>Win</b> & "q" <img src=x>\nsecond line'


@pytest.fixture
def pane(qtbot: QtBot) -> BriefDetailPane:
    pane = BriefDetailPane()
    qtbot.addWidget(pane)
    pane.resize(420, 700)
    return pane


def show(pane: BriefDetailPane, key: str) -> None:
    brief = mockup_digest()
    item = next(item for item in brief.digest.items if item.message_key == key)
    pane.show_item(
        item,
        account_email=ACCOUNT,
        timezone_name=ZONE,
        links=brief.links.get(key, ()),
        proposals=brief.proposals.get(key, ()),
    )


def button(pane: QWidget, text: str, occurrence: int = 0) -> QPushButton:
    matches = [b for b in pane.findChildren(QPushButton) if b.text() == text]
    return matches[occurrence]


def label_texts(pane: QWidget) -> list[str]:
    return [label.text() for label in pane.findChildren(QLabel) if label.text()]


def test_mockup_item_reads_like_the_mockup(pane: BriefDetailPane) -> None:
    show(pane, "priya")
    texts = label_texts(pane)
    assert pane.title is not None
    assert pane.title.text() == "Q3 budget: approval needed by Friday"
    assert "Priya Shah" in texts
    assert "Due Fri Oct 9, 17:00" in texts
    assert "Continues: “Finance review prep”" in texts
    assert "Yours. Target Thu Oct 8, one working day before the deadline." in texts
    assert "Yours. No deadline stated." in texts
    assert [b.text() for b in pane.findChildren(QPushButton)] == [
        "Accept",
        "Dismiss",
        "Add to “Finance review prep”",
        "Accept",
        "Dismiss",
        "Add to “Finance review prep”",
        "Draft a reply",
        "Open in Gmail",
    ]
    assert all(b.property("variant") == "outline" for b in pane.findChildren(QPushButton))
    assert not any(b.autoDefault() for b in pane.findChildren(QPushButton))


def test_suggestion_buttons_carry_their_own_ids(pane: BriefDetailPane) -> None:
    show(pane, "priya")
    decisions = QSignalSpy(pane.suggestion_requested)
    into = QSignalSpy(pane.accept_into_requested)
    button(pane, "Accept").click()
    button(pane, "Dismiss", 1).click()
    button(pane, "Accept", 1).click()
    button(pane, "Add to “Finance review prep”", 1).click()
    assert [decisions.at(n) for n in range(decisions.count())] == [
        [ACCEPT, 11],
        [DISMISS, 12],
        [ACCEPT, 12],
    ]
    assert into.count() == 1 and into.at(0) == [12, FINANCE_ACTION, 3]


def test_proposal_buttons_carry_the_proposal_and_revision(pane: BriefDetailPane) -> None:
    show(pane, "marco")
    spy = QSignalSpy(pane.proposal_requested)
    assert "Proposes for “Send the conference deck”: “Can we move this to next Monday?”" in (
        label_texts(pane)
    )
    button(pane, "Set the deadline to 2026-10-12").click()
    button(pane, "Dismiss").click()
    assert spy.at(0) == [APPLY, 21, 2]
    assert spy.at(1) == [DISMISS, 21, 2]


def test_reply_and_source_requests(pane: BriefDetailPane) -> None:
    show(pane, "sam")
    replies = QSignalSpy(pane.reply_requested)
    sources = QSignalSpy(pane.source_requested)
    button(pane, "Draft a reply").click()
    button(pane, "Open in Gmail").click()
    assert replies.at(0) == [ACCOUNT, "sam"]
    assert sources.at(0) == ["https://mail.google.com/mail/u/0/#inbox/sam"]


@pytest.mark.parametrize(
    "url",
    [
        "https://outlook.office.com/mail/id/message-1",
        "http://mail.google.com/mail/u/0/#inbox/x",
        "https://mail.google.com.evil.example/x",
    ],
)
def test_open_in_gmail_only_for_gmail_https_links(pane: BriefDetailPane, url: str) -> None:
    pane.show_item(make_digest_item(source_url=url), account_email=ACCOUNT, timezone_name=ZONE)
    assert [b.text() for b in pane.findChildren(QPushButton)] == ["Draft a reply"]


def test_states_and_empty(pane: BriefDetailPane) -> None:
    accepted = SuggestionView(
        suggestion_id=5, state=SuggestionState.ACCEPTED, suggestion=make_suggestion()
    )
    dismissed = SuggestionView(
        suggestion_id=6, state=SuggestionState.DISMISSED, suggestion=make_suggestion()
    )
    done = make_proposal(state=ProposalState.APPLIED)
    pane.show_item(
        make_digest_item(suggestions=(accepted, dismissed), action_text="Approve the proposal."),
        account_email=ACCOUNT,
        timezone_name=ZONE,
        proposals=(done,),
    )
    texts = label_texts(pane)
    assert "Accepted: Approve the proposal" in texts
    assert texts.count("Approve the proposal.") == 1  # Action text equal to the summary.
    assert [b.text() for b in pane.findChildren(QPushButton)] == ["Draft a reply"]
    pane.show_empty()
    assert pane.title is None
    assert label_texts(pane) == ["Select an email to see its summary and suggestions."]


def test_deadline_and_meta_texts() -> None:
    unresolved = make_digest_item(
        deadline_text="end of week",
        deadline_precision=DeadlinePrecision.UNRESOLVED,
        deadline_at_utc=None,
    )
    dated = make_digest_item(
        deadline_text="Oct 9",
        deadline_precision=DeadlinePrecision.DATE,
        deadline_date=date(2026, 10, 9),
        deadline_at_utc=None,
    )
    assert item_deadline_text(unresolved, TORONTO) == "Due “end of week”"
    assert item_deadline_text(dated, TORONTO) == "Due Fri Oct 9"
    assert item_deadline_text(make_digest_item(), TORONTO) is None
    waiting = SuggestionView(
        suggestion_id=1,
        state=SuggestionState.PENDING,
        suggestion=make_suggestion(
            ownership="waiting_for",
            deadline_text="Friday 5pm",
            deadline_precision=DeadlinePrecision.DATETIME,
            deadline_date=date(2026, 10, 9),
            deadline_at_utc=datetime(2026, 10, 9, 21, 0, tzinfo=UTC),
            deadline_timezone=ZONE,
        ),
    )
    assert suggestion_meta(waiting, TORONTO) == "Waiting for someone. Due 2026-10-09 17:00."


def test_hostile_text_stays_literal_plain_text(pane: BriefDetailPane) -> None:
    brief = mockup_digest()
    links = tuple(link.model_copy(update={"title": HOSTILE}) for link in brief.links["priya"])
    proposal = make_proposal(action_title=HOSTILE, evidence=HOSTILE, action_public_id=DECK_ACTION)
    original = brief.digest.items[0]
    hostile_views = tuple(
        view.model_copy(
            update={"suggestion": view.suggestion.model_copy(update={"title": HOSTILE})}
        )
        for view in original.suggestions
    )
    item = original.model_copy(
        update={
            "subject": HOSTILE,
            "summary": HOSTILE,
            "action_text": HOSTILE + " now",
            "sender": EmailContact(name=HOSTILE, address="x@example.com"),
            "suggestions": hostile_views,
        }
    )
    pane.show_item(
        item, account_email=ACCOUNT, timezone_name=ZONE, links=links, proposals=(proposal,)
    )
    labels = [label for label in pane.findChildren(QLabel) if "<b>Win</b>" in label.text()]
    assert len(labels) >= 6  # Subject, sender, summary, action, continues, titles, proposal.
    assert all(label.textFormat() is Qt.TextFormat.PlainText for label in labels)
    assert pane.title is not None and pane.title.text() == HOSTILE
    into = [b for b in pane.findChildren(QPushButton) if b.text().startswith("Add to")]
    assert into and all("&&" in b.text() and "<b>Win</b>" in b.text() for b in into)
    for widget in pane.findChildren(QWidget):
        assert "<b>" not in widget.toolTip()
        assert "<b>" not in widget.accessibleDescription()
