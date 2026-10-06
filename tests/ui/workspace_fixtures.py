"""The approved mockup's brief (docs/ui/mockup-three-pane-dark.png) as validated models."""

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import NamedTuple

from mailbrief.domain.actions import (
    ActionProposal,
    SuggestionState,
    SuggestionView,
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
from tests.factories import fingerprint_of, make_digest_item, make_proposal, make_suggestion

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
            action_text="Approve the revised Q3 budget by Friday at 17:00.",
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
