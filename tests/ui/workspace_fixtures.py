"""The approved mockup's brief (docs/ui/mockup-three-pane-dark.png) as validated models."""

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import NamedTuple

from mailbrief.domain.actions import (
    Action,
    ActionFilter,
    ActionProposal,
    ActionStatus,
    ActionStep,
    SuggestionState,
    SuggestionView,
    ThreadActivity,
    ThreadLink,
)
from mailbrief.domain.analysis import (
    ActionOwnership,
    DeadlinePrecision,
    FollowUpKind,
    TargetReason,
)
from mailbrief.domain.digests import (
    DailyDigest,
    DigestCoverage,
    DigestItem,
    DigestSection,
    DigestStatus,
)
from mailbrief.domain.messages import EmailContact
from tests.factories import (
    fingerprint_of,
    make_action,
    make_digest_item,
    make_proposal,
    make_suggestion,
)

ZONE = "America/Toronto"
ACCOUNT = "owner@example.com"
FINANCE_ACTION = "5b1f3c2e-8d4a-4e6b-9c7d-1a2b3c4d5e6f"
DECK_ACTION = "7e2d9a4b-3c1f-4a8e-b6d5-0f9e8d7c6b5a"


def gmail(key: str) -> str:
    return f"https://mail.google.com/mail/u/0/#inbox/{key}"


class MockupBrief(NamedTuple):
    digest: DailyDigest
    links: Mapping[str, Sequence[ThreadLink]]
    proposals: Mapping[str, Sequence[ActionProposal]]


def item(position: int, key: str, section: DigestSection, **overrides: object) -> DigestItem:
    values: dict[str, object] = {
        "message_key": key,
        "section": section,
        "position": position,
        "action_text": None,
        "deadline_at_utc": None,
        "source_url": gmail(key),
    }
    values.update(overrides)
    return make_digest_item(**values)


def mockup_digest() -> MockupBrief:
    approve = SuggestionView(
        suggestion_id=11,
        state=SuggestionState.PENDING,
        suggestion=make_suggestion(
            position=0,
            title="Approve the Q3 budget",
            ownership=ActionOwnership.MINE,
            deadline_text="by Friday at 5pm",
            deadline_precision=DeadlinePrecision.DATETIME,
            deadline_date=date(2026, 10, 9),
            deadline_at_utc=datetime(2026, 10, 9, 21, 0, tzinfo=UTC),
            deadline_timezone=ZONE,
            suggested_target_date=date(2026, 10, 8),
            target_reason=TargetReason.WORKING_DAY_BEFORE,
            evidence="approve the revised Q3 budget by Friday at 5pm",
            fingerprint=fingerprint_of("approve the q3 budget"),
        ),
    )
    questions = SuggestionView(
        suggestion_id=12,
        state=SuggestionState.PENDING,
        suggestion=make_suggestion(
            position=1,
            title="Send questions on the travel line",
            ownership=ActionOwnership.MINE,
            evidence="let me know if you have questions on the travel line",
            fingerprint=fingerprint_of("send questions on the travel line"),
        ),
    )
    items = (
        item(
            0,
            "priya",
            DigestSection.ACTIONS,
            subject="Q3 budget: approval needed by Friday",
            sender=EmailContact(name="Priya Shah", address="priya@example.com"),
            summary=(
                "Priya needs your approval of the revised Q3 budget before Friday's finance "
                "review. Two line items changed since last month."
            ),
            deadline_text="by Friday at 5pm",
            deadline_precision=DeadlinePrecision.DATETIME,
            deadline_date=date(2026, 10, 9),
            deadline_at_utc=datetime(2026, 10, 9, 21, 0, tzinfo=UTC),
            suggestions=(approve, questions),
        ),
        item(
            1,
            "lena",
            DigestSection.ACTIONS,
            subject="Contract renewal options",
            sender=EmailContact(name="Lena Park", address="lena@example.com"),
            summary="Lena lists three renewal options for the vendor contract.",
        ),
        item(
            2,
            "sam",
            DigestSection.DEADLINES,
            subject="Invoice 2041",
            sender=EmailContact(name="Sam Okafor", address="sam@example.com"),
            summary="Invoice 2041 for September's design work is due on October 9.",
            deadline_text="October 9",
            deadline_precision=DeadlinePrecision.DATE,
            deadline_date=date(2026, 10, 9),
        ),
        item(
            3,
            "marco",
            DigestSection.FOLLOW_UPS,
            subject="Re: Conference deck",
            sender=EmailContact(name="Marco Diaz", address="marco@example.com"),
            summary="Marco asks to move the conference deck to next Monday.",
        ),
        item(
            4,
            "facilities",
            DigestSection.HIGHLIGHTS,
            subject="Office closed Monday",
            sender=EmailContact(name="Facilities", address="facilities@example.com"),
            summary="The office is closed on Monday for the holiday.",
        ),
    )
    digest = DailyDigest(
        account_id=ACCOUNT,
        local_date=date(2026, 10, 6),
        timezone_name=ZONE,
        generated_at_utc=datetime(2026, 10, 6, 13, 14, tzinfo=UTC),
        status=DigestStatus.COMPLETE,
        items=items,
        coverage=DigestCoverage(
            sync_complete=True, shortlisted=5, analyzed=5, reused=0, failed=0, skipped=0
        ),
    )
    finance = ThreadLink(
        public_id=FINANCE_ACTION,
        title="Finance review prep",
        revision=3,
        ownership=ActionOwnership.MINE,
        is_source=False,
    )
    deck = make_proposal(
        id=21,
        action_public_id=DECK_ACTION,
        action_title="Send the conference deck",
        action_revision=2,
        kind=FollowUpKind.NEW_DEADLINE,
        evidence="Can we move this to next Monday?",
        deadline_text="next Monday",
        deadline_precision=DeadlinePrecision.DATE,
        deadline_date=date(2026, 10, 12),
        deadline_timezone=ZONE,
        provider_message_id="marco",
        subject="Re: Conference deck",
        sender_address="marco@example.com",
        received_at_utc=datetime(2026, 10, 6, 12, 30, tzinfo=UTC),
        web_link=gmail("marco"),
        created_at_utc=datetime(2026, 10, 6, 13, 14, tzinfo=UTC),
    )
    return MockupBrief(digest, {"priya": (finance,)}, {"marco": (deck,)})


ACTIONS_NOW = datetime(2026, 10, 6, 13, 14, tzinfo=UTC)  # 09:14 in Toronto, the brief's day.


def _source(
    key: str, subject: str, sender: str, day: int, *, available: bool = True
) -> dict[str, object]:
    return {
        "provider_message_id": key,
        "subject": subject,
        "sender_address": sender,
        "web_link": gmail(key),
        "received_at_utc": datetime(2026, 10, day, 14, tzinfo=UTC),
        "available": available,
        "in_inbox": True if available else None,
    }


def _steps(*texts: str, done: int = 0) -> tuple[ActionStep, ...]:
    return tuple(
        ActionStep(step_id=number + 1, position=number, text=text, done=number < done)
        for number, text in enumerate(texts)
    )


def mockup_actions() -> dict[ActionFilter, tuple[Action, ...]]:
    """Open, waiting and completed actions for the Actions page renders: overdue, thread
    activity, a pending proposal and plan steps among them."""
    created = datetime(2026, 10, 2, 13, tzinfo=UTC)
    invoice = make_action(
        public_id=FINANCE_ACTION,
        title="Pay the Q3 contractor invoice",
        deadline_text="by Monday",
        deadline_precision=DeadlinePrecision.DATE,
        deadline_date=date(2026, 10, 5),
        deadline_timezone=ZONE,
        target_date=date(2026, 10, 2),
        suggested_target_date=date(2026, 10, 2),
        target_reason=TargetReason.WORKING_DAY_BEFORE,
        created_at_utc=created,
        updated_at_utc=created,
        steps=_steps("Check the hours against the timesheet", "Approve in the portal", done=1),
        sources=(_source("inv-1", "Invoice #2291 for September", "billing@northwind.example", 2),),
    )
    deck = make_action(
        public_id=DECK_ACTION,
        title="Send Priya the revised Q3 deck",
        deadline_text="Thursday 5 PM",
        deadline_precision=DeadlinePrecision.DATETIME,
        deadline_date=date(2026, 10, 8),
        deadline_at_utc=datetime(2026, 10, 8, 21, tzinfo=UTC),
        deadline_timezone=ZONE,
        target_date=date(2026, 10, 7),
        created_at_utc=created,
        updated_at_utc=created,
        notes="Use the new revenue chart. Priya wants the appendix trimmed to two slides.",
        steps=_steps(
            "Update the revenue chart",
            "Trim the appendix",
            "Add the hiring plan",
            "Ask Sam to proofread",
            "Send to Priya",
            done=2,
        ),
        sources=(_source("deck-1", "Q3 deck: a few changes", "priya@example.com", 5),),
        thread=ThreadActivity(
            new_messages=2,
            latest_at_utc=datetime(2026, 10, 6, 12, 2, tzinfo=UTC),
            latest_sender="Priya Raman",
        ),
    )
    venue = make_action(
        public_id="2a4c6e8f-1b3d-4f5a-8c7e-9d0b1a2c3e4f",
        title="Book the offsite venue",
        created_at_utc=ACTIONS_NOW,
        updated_at_utc=ACTIONS_NOW,
        sources=(_source("venue-1", "Offsite: venue options", "ops@example.com", 6),),
        proposals=(
            make_proposal(
                action_public_id="2a4c6e8f-1b3d-4f5a-8c7e-9d0b1a2c3e4f",
                action_title="Book the offsite venue",
                kind=FollowUpKind.NEW_DEADLINE,
                deadline_text="Friday",
                deadline_precision=DeadlinePrecision.DATE,
                deadline_date=date(2026, 10, 9),
                deadline_timezone=ZONE,
                evidence="Can we lock it in by Friday?",
            ),
        ),
    )
    contract = make_action(
        public_id="3b5d7f9a-2c4e-4a6b-8d0f-1e2a3b4c5d6e",
        title="Signed contract back from Acme legal",
        ownership=ActionOwnership.WAITING_FOR,
        target_date=date(2026, 10, 9),
        created_at_utc=created,
        updated_at_utc=created,
        sources=(_source("acme-1", "Re: MSA redlines", "legal@acme.example", 1, available=False),),
        thread=ThreadActivity(owner_replied_at_utc=datetime(2026, 10, 5, 15, tzinfo=UTC)),
    )
    quote = make_action(
        public_id="4c6e8a0b-3d5f-4b7c-9e1a-2f3b4c5d6e7f",
        title="Quote from the print shop",
        ownership=ActionOwnership.WAITING_FOR,
        created_at_utc=ACTIONS_NOW,
        updated_at_utc=ACTIONS_NOW,
    )
    done_at = datetime(2026, 10, 5, 20, tzinfo=UTC)
    expenses = make_action(
        public_id="5d7f9b1c-4e6a-4c8d-8f2b-3a4b5c6d7e8f",
        title="Submit September expenses",
        status=ActionStatus.COMPLETED,
        completed_at_utc=done_at,
        created_at_utc=created,
        updated_at_utc=done_at,
        steps=_steps("Scan the receipts", "File the report", done=2),
    )
    return {
        ActionFilter.OPEN: (invoice, deck, venue),
        ActionFilter.WAITING: (contract, quote),
        ActionFilter.COMPLETED: (expenses,),
    }
