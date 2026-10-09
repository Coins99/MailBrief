"""The detail pane: requests carry the right ids, Gmail links only, and hostile text stays
literal plain text."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
import shiboken6
from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QLabel, QPushButton, QWidget
from pytestqt.qtbot import QtBot

from mailbrief.domain.actions import ProposalState, SuggestionState, SuggestionView, ThreadLink
from mailbrief.domain.analysis import ActionOwnership, DeadlinePrecision, FollowUpKind
from mailbrief.domain.messages import EmailContact
from mailbrief.ui.brief_detail import (
    ACCEPT,
    APPLY,
    DISMISS,
    BriefDetailPane,
    item_deadline_text,
    suggestion_meta,
)
from mailbrief.ui.hairline import HairlineFrame
from mailbrief.ui.labels import outline_button
from mailbrief.ui.theme import SMALL_PX
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


@pytest.mark.parametrize(
    ("sender", "shown"),
    [
        # A display name that poses as someone else's address shows only the real one.
        (
            EmailContact(name="Priya Shah <priya@corp.example>", address="attacker@evil.example"),
            "attacker@evil.example",
        ),
        (EmailContact(name="", address="sam@example.com"), "sam@example.com"),
        (EmailContact(name=None, address="sam@example.com"), "sam@example.com"),
    ],
)
def test_the_sender_line_never_hides_the_address(
    pane: BriefDetailPane, sender: EmailContact, shown: str
) -> None:
    pane.show_item(make_digest_item(sender=sender), account_email=ACCOUNT, timezone_name=ZONE)
    line = pane.findChild(QLabel, "detailSender")
    assert line is not None and line.text() == shown


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
    assert "Priya Shah <priya@example.com>" in texts  # The name and its address.
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


def test_long_titles_shorten_buttons_but_keep_the_full_name(pane: BriefDetailPane) -> None:
    brief = mockup_digest()
    long_title = ("Quarterly finance review preparation " * 4)[:120]
    links = tuple(link.model_copy(update={"title": long_title}) for link in brief.links["priya"])
    proposal = make_proposal(
        evidence="Cancelled",
        action_title=long_title,
        kind="new_deadline",
        deadline_text="the first working day after the long weekend in the spring term",
        deadline_precision=DeadlinePrecision.UNRESOLVED,
    )
    pane.show_item(
        brief.digest.items[0],
        account_email=ACCOUNT,
        timezone_name=ZONE,
        links=links,
        proposals=(proposal,),
    )
    into = [b for b in pane.findChildren(QPushButton) if b.text().startswith("Add to")]
    assert into
    for button in into:
        assert len(button.text().replace("&&", "&")) == 40
        assert button.text().endswith("…")
        assert button.accessibleName() == f"Add to “{long_title}”"
        assert button.toolTip() == ""
    apply = button_with_prefix(pane, "Set the deadline")
    assert len(apply.text().replace("&&", "&")) == 40
    assert apply.accessibleName().startswith("Set the deadline to “the first working day")
    content = pane.widget()
    assert content is not None
    narrowest = content.minimumSizeHint().width()
    # Font metrics differ by platform, so compare with a button built the same way: one
    # showing the whole title would be wider than all of the pane's content.
    whole = outline_button(f"Add to “{long_title}”", "addToButton")
    whole.setParent(content)
    assert narrowest < whole.minimumSizeHint().width()


def button_with_prefix(pane: QWidget, prefix: str) -> QPushButton:
    return next(b for b in pane.findChildren(QPushButton) if b.text().startswith(prefix))


# Continuations, Add to, proposals and buttons in the pane.

TRACKED = ThreadLink(
    public_id="22222222-2222-4222-8222-222222222222",
    title='<a href="https://evil.example">Chase</a> the <b>deck</b>',
    revision=3,
    ownership=ActionOwnership.WAITING_FOR,
    is_source=False,
)
OWN = ThreadLink(
    public_id="33333333-3333-4333-8333-333333333333",
    title="Book the room",
    revision=1,
    ownership=ActionOwnership.MINE,
    is_source=True,
)
PENDING_SUGGESTION = SuggestionView(
    suggestion_id=7, state=SuggestionState.PENDING, suggestion=make_suggestion()
)


def show_links(pane: BriefDetailPane, *views: SuggestionView) -> None:
    pane.show_item(
        make_digest_item(suggestions=views or (PENDING_SUGGESTION,)),
        account_email=ACCOUNT,
        timezone_name=ZONE,
        links=(TRACKED, OWN),
    )


def add_to_buttons(pane: QWidget) -> list[QPushButton]:
    return [b for b in pane.findChildren(QPushButton) if b.text().startswith("Add to")]


def test_continues_names_only_actions_the_email_is_not_a_source_of(
    pane: BriefDetailPane,
) -> None:
    show_links(pane)
    texts = label_texts(pane)
    assert f"Continues: “{TRACKED.title}”" in texts  # Its markup stays text.
    assert "Continues: “Book the room”" not in texts  # The email is already its source.
    requests = QSignalSpy(pane.accept_into_requested)
    for into in add_to_buttons(pane):
        into.click()
    assert [requests.at(n) for n in range(requests.count())] == [
        [7, TRACKED.public_id, 3],
        [7, OWN.public_id, 1],
    ]
    for label in pane.findChildren(QLabel):
        assert label.textFormat() is Qt.TextFormat.PlainText
        assert not label.openExternalLinks()


def test_only_pending_suggestions_offer_add_to(pane: BriefDetailPane) -> None:
    accepted = PENDING_SUGGESTION.model_copy(
        update={"state": SuggestionState.ACCEPTED, "action_public_id": OWN.public_id}
    )
    show_links(pane, accepted)
    assert f"Continues: “{TRACKED.title}”" in label_texts(pane)
    assert add_to_buttons(pane) == []


def test_another_email_replaces_the_old_buttons(pane: BriefDetailPane) -> None:
    show_links(pane)
    old = add_to_buttons(pane)[0]
    requests = QSignalSpy(pane.accept_into_requested)
    show(pane, "lena")  # No suggestions, links or proposals.
    assert not shiboken6.isValid(old)
    assert add_to_buttons(pane) == []
    assert [b.text() for b in pane.findChildren(QPushButton)] == ["Draft a reply", "Open in Gmail"]
    assert requests.count() == 0
    show(pane, "marco")
    assert pane.findChild(HairlineFrame, "proposalCard") is not None
    pane.show_item(
        mockup_digest().digest.items[3], account_email=ACCOUNT, timezone_name=ZONE
    )  # The same email without its proposals.
    assert pane.findChild(HairlineFrame, "proposalCard") is None


def test_pending_proposals_sit_above_the_footer_in_the_brief_s_zone(
    pane: BriefDetailPane,
) -> None:
    exact = make_proposal(
        id=3,
        action_revision=4,
        kind=FollowUpKind.NEW_DEADLINE,
        deadline_text="Monday 5 PM",
        deadline_precision=DeadlinePrecision.DATETIME,
        deadline_date=date(2026, 10, 5),
        deadline_at_utc=datetime(2026, 10, 5, 21, 0, tzinfo=UTC),
        deadline_timezone="America/Toronto",
    )
    delivered = make_proposal(id=9, action_revision=7, kind=FollowUpKind.DELIVERED)
    applied = make_proposal(id=4, state=ProposalState.APPLIED, evidence="Quietly dropped")
    pane.show_item(
        make_digest_item(),
        account_email=ACCOUNT,
        timezone_name="Asia/Tokyo",  # 21:00 UTC is 06:00 the next day there.
        proposals=(exact, applied, delivered),
    )
    cards = pane.findChildren(HairlineFrame, "proposalCard")
    assert len(cards) == 2  # Only pending proposals.
    assert "Quietly dropped" not in " ".join(label_texts(pane))
    buttons = [b.text() for b in pane.findChildren(QPushButton)]
    assert buttons == [
        "Set the deadline to 2026-10-06 06:00",
        "Dismiss",
        "Complete it (delivered)",
        "Dismiss",
        "Draft a reply",
    ]
    spy = QSignalSpy(pane.proposal_requested)
    for proposal_button in pane.findChildren(QPushButton)[:4]:
        proposal_button.click()
    assert [spy.at(n) for n in range(spy.count())] == [
        [APPLY, 3, 4],
        [DISMISS, 3, 4],
        [APPLY, 9, 7],
        [DISMISS, 9, 7],
    ]


def test_a_hostile_proposal_deadline_stays_text(pane: BriefDetailPane) -> None:
    proposal = make_proposal(
        kind=FollowUpKind.NEW_DEADLINE,
        deadline_text=HOSTILE,
        deadline_precision=DeadlinePrecision.UNRESOLVED,
    )
    pane.show_item(
        make_digest_item(), account_email=ACCOUNT, timezone_name=ZONE, proposals=(proposal,)
    )
    effect = pane.findChildren(QPushButton)[0]
    assert "&&" in effect.text() and "<b>Win</b>" in effect.text()  # No mnemonic, no markup.
    assert effect.accessibleName().startswith("Set the deadline to “<b>Win</b> & ")


def test_an_unresolved_suggestion_deadline_shows_its_words() -> None:
    soon = SuggestionView(
        suggestion_id=12,
        state=SuggestionState.PENDING,
        suggestion=make_suggestion(
            deadline_text="<b>soon</b>", deadline_precision=DeadlinePrecision.UNRESOLVED
        ),
    )
    assert suggestion_meta(soon, TORONTO) == "Yours. Due “<b>soon</b>”."


def test_every_button_can_be_reached_from_the_keyboard(pane: BriefDetailPane) -> None:
    show(pane, "priya")
    buttons = pane.findChildren(QPushButton)
    assert buttons
    for reachable in buttons:
        assert reachable.focusPolicy() & Qt.FocusPolicy.TabFocus
        assert not reachable.autoDefault()


def plan_suggestion(steps: tuple[str, ...], state: SuggestionState) -> SuggestionView:
    return SuggestionView(suggestion_id=30, state=state, suggestion=make_suggestion(steps=steps))


def test_a_pending_suggestion_shows_its_plan(pane: BriefDetailPane) -> None:
    steps = ("Read it", "Check totals", "Ask Sam", "Approve", "Tell finance")
    pane.show_item(
        make_digest_item(suggestions=(plan_suggestion(steps, SuggestionState.PENDING),)),
        account_email=ACCOUNT,
        timezone_name=ZONE,
    )
    card = pane.findChild(HairlineFrame, "suggestionCard")
    assert card is not None
    lines = [label for label in card.findChildren(QLabel) if label.text().startswith("· ")]
    assert [label.text() for label in lines] == [f"· {step}" for step in steps]
    for line in lines:
        assert line.property("tone") == "secondary" and line.font().pixelSize() == SMALL_PX
        assert line.textFormat() is Qt.TextFormat.PlainText


def test_a_hostile_plan_step_stays_literal(pane: BriefDetailPane) -> None:
    hostile = plan_suggestion(("<b>Win</b> & <a href=x>go</a>",), SuggestionState.PENDING)
    pane.show_item(
        make_digest_item(suggestions=(hostile,)),
        account_email=ACCOUNT,
        timezone_name=ZONE,
    )
    assert "· <b>Win</b> & <a href=x>go</a>" in label_texts(pane)


def test_an_accepted_suggestion_shows_no_plan(pane: BriefDetailPane) -> None:
    pane.show_item(
        make_digest_item(suggestions=(plan_suggestion(("Read it",), SuggestionState.ACCEPTED),)),
        account_email=ACCOUNT,
        timezone_name=ZONE,
    )
    assert "· Read it" not in label_texts(pane)
