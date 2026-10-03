"""Small typed factories shared by unit tests."""

import hashlib
from datetime import UTC, date, datetime

from mailbrief.domain.actions import Action, ActionProposal, ActionStatus, ProposalState
from mailbrief.domain.analysis import (
    ActionOwnership,
    ActionSuggestion,
    AnalysisCategory,
    DeadlinePrecision,
    FollowUpKind,
    MessageAnalysis,
)
from mailbrief.domain.briefs import AutoSendStatus, TransmissionPreview
from mailbrief.domain.digests import DigestItem, DigestSection
from mailbrief.domain.messages import (
    EmailContact,
    MessageImportance,
    NormalizedMessage,
    ProviderKind,
)


def make_message(**overrides: object) -> NormalizedMessage:
    """Build valid normalized message metadata with optional overrides."""
    values: dict[str, object] = {
        "provider": ProviderKind.MICROSOFT,
        "provider_account_id": "account-1",
        "provider_message_id": "message-1",
        "internet_message_id": "<message-1@example.com>",
        "conversation_id": "conversation-1",
        "subject": "Approval needed by Friday",
        "sender": EmailContact(name="Alex", address="alex@example.com"),
        "to_recipients": (EmailContact(name="Taylor", address="taylor@example.com"),),
        "received_at_utc": datetime(2026, 8, 31, 14, 30, tzinfo=UTC),
        "is_read": False,
        "importance": MessageImportance.HIGH,
        "has_attachments": False,
        "body_preview": "Please approve the attached proposal by Friday.",
        "web_link": "https://outlook.office.com/mail/id/message-1",
    }
    values.update(overrides)
    return NormalizedMessage.model_validate(values)


def make_analysis(**overrides: object) -> MessageAnalysis:
    """Build a valid structured analysis with a consistent exact deadline by default."""
    values: dict[str, object] = {
        "message_key": "local-1",
        "category": AnalysisCategory.ACTION,
        "summary": "Approve the proposal.",
        "action_required": True,
        "action_text": "Approve the proposal by Friday.",
        "deadline_text": "Friday 5 PM",
        "deadline_precision": DeadlinePrecision.DATETIME,
        "deadline_date": date(2026, 9, 4),
        "deadline_timezone": "America/Toronto",
        "deadline_at_utc": datetime(2026, 9, 4, 21, 0, tzinfo=UTC),
        "confidence": 0.9,
        "evidence": "Please approve the attached proposal by Friday.",
    }
    values.update(overrides)
    return MessageAnalysis.model_validate(values)


def fingerprint_of(text: str) -> str:
    """A well-formed suggestion fingerprint; tests only need it to be distinct per text."""
    return hashlib.sha256(text.encode()).hexdigest()


def make_suggestion(**overrides: object) -> ActionSuggestion:
    """Build a valid suggestion without a deadline or target by default."""
    values: dict[str, object] = {
        "position": 0,
        "title": "Approve the proposal",
        "ownership": ActionOwnership.MINE,
        "steps": ("Read the proposal",),
        "evidence": "Please approve the attached proposal",
        "fingerprint": fingerprint_of("approve the proposal"),
    }
    values.update(overrides)
    return ActionSuggestion.model_validate(values)


def make_action(**overrides: object) -> Action:
    """Build a valid open action without a deadline by default."""
    values: dict[str, object] = {
        "public_id": "0c5e2c1d-6b8e-4f55-9d0e-2a7f3b9c1e44",
        "title": "Approve the proposal",
        "ownership": ActionOwnership.MINE,
        "status": ActionStatus.OPEN,
        "created_at_utc": datetime(2026, 8, 31, 14, 30, tzinfo=UTC),
        "updated_at_utc": datetime(2026, 8, 31, 14, 30, tzinfo=UTC),
        "revision": 1,
    }
    values.update(overrides)
    return Action.model_validate(values)


def make_proposal(**overrides: object) -> ActionProposal:
    """Build a valid pending "cancelled" proposal for make_action's action by default."""
    values: dict[str, object] = {
        "id": 3,
        "action_public_id": "0c5e2c1d-6b8e-4f55-9d0e-2a7f3b9c1e44",
        "action_title": "Approve the proposal",
        "action_revision": 1,
        "kind": FollowUpKind.CANCELLED,
        "state": ProposalState.PENDING,
        "evidence": "No longer needed, thanks",
        "provider_message_id": "reply-1",
        "subject": "Re: deck",
        "sender_address": "sam@example.com",
        "received_at_utc": datetime(2026, 9, 3, 13, 0, tzinfo=UTC),
        "web_link": "https://mail.google.com/mail/u/0/#inbox/reply-1",
        "created_at_utc": datetime(2026, 9, 3, 14, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return ActionProposal.model_validate(values)


def make_auto_send(**overrides: object) -> AutoSendStatus:
    """The automatic-analysis permission of owner@example.com: off, with its disclosure."""
    values: dict[str, object] = {
        "account_email": "owner@example.com",
        "limit": 0,
        "granted_at_utc": None,
        "disclosure": TransmissionPreview(
            provider_name="groq",
            model_name="test-model",
            message_count=1,
            truncated_count=0,
            reused_count=0,
            first_use=False,
            body_character_limit=4_000,
            privacy_notice="Enable Zero Data Retention.",
        ),
    }
    values.update(overrides)
    return AutoSendStatus.model_validate(values)


def make_digest_item(**overrides: object) -> DigestItem:
    """Build a valid digest item with optional overrides."""
    values: dict[str, object] = {
        "message_key": "local-1",
        "section": DigestSection.ACTIONS,
        "position": 0,
        "subject": "Approval needed by Friday",
        "sender": EmailContact(name="Alex", address="alex@example.com"),
        "summary": "Approve the proposal.",
        "action_text": "Approve the proposal by Friday.",
        "deadline_at_utc": datetime(2026, 9, 4, 21, 0, tzinfo=UTC),
        "source_url": "https://outlook.office.com/mail/id/message-1",
    }
    values.update(overrides)
    return DigestItem.model_validate(values)


TEST_LOCAL_DATE = date(2026, 8, 31)
TEST_GENERATED_AT = datetime(2026, 8, 31, 16, 0, tzinfo=UTC)
