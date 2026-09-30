"""The automatic-analysis permission on the active consent: the one function the desktop and
the CLI share, and the disclosure it is given under (ADR 0017)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.briefs import AUTO_SEND_LIMIT_MAX, TransmissionPreview
from mailbrief.domain.messages import AccountIdentity, ProviderKind
from mailbrief.errors import ConfigurationError
from mailbrief.services.brief import disclosure_lines, permission_preview, permission_sentence
from mailbrief.services.consent import NO_CONSENT, auto_send_permission, set_auto_send
from mailbrief.storage.database import Database
from mailbrief.storage.repositories import AccountRepository, ConsentRepository
from mailbrief.storage.tables import AccountTable

AT = datetime(2026, 9, 30, 13, tzinfo=UTC)
LATER = AT + timedelta(hours=1)


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.from_path(tmp_path / "consent.sqlite3")
    await database.create_schema_for_tests()
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
async def session(database: Database) -> AsyncIterator[AsyncSession]:
    async with database.session() as active:
        yield active


async def consent(
    session: AsyncSession,
    name: str = "me",
    *,
    kind: ProviderKind = ProviderKind.GMAIL,
    provider: str = "groq",
    version: str = "2",
    granted: datetime = AT,
) -> AccountTable:
    owner = await AccountRepository(session).upsert(
        AccountIdentity(
            provider=kind, provider_account_id=name, email_address=f"{name}@example.com"
        )
    )
    await ConsentRepository(session).grant(owner.id, provider, version, granted)
    await session.commit()
    return owner


async def test_with_no_consent_there_is_no_permission_to_read_or_give(
    session: AsyncSession,
) -> None:
    assert await auto_send_permission(session, provider="groq", version="2") is None
    with pytest.raises(ConfigurationError) as caught:
        await set_auto_send(session, 3, provider="groq", version="2")
    assert str(caught.value) == "Analyze once with Sync and review to give consent first."
    assert str(caught.value) == NO_CONSENT


async def test_a_consent_starts_with_no_permission_then_takes_one_and_gives_it_up(
    session: AsyncSession,
) -> None:
    await consent(session)
    now = [AT]

    start = await auto_send_permission(session, provider="groq", version="2")
    assert start is not None and (start.account_email, start.limit) == ("me@example.com", 0)
    assert start.granted_at_utc is None

    given = await set_auto_send(session, 4, provider="groq", version="2", clock=lambda: now[0])
    assert (given.limit, given.granted_at_utc) == (4, AT)

    now[0] = LATER
    changed = await set_auto_send(session, 2, provider="groq", version="2", clock=lambda: now[0])
    assert (changed.limit, changed.granted_at_utc) == (2, LATER)  # Given again: the new time.
    read = await auto_send_permission(session, provider="groq", version="2")
    assert read == changed

    off = await set_auto_send(session, 0, provider="groq", version="2", clock=lambda: now[0])
    assert (off.limit, off.granted_at_utc) == (0, None)
    assert await auto_send_permission(session, provider="groq", version="2") == off


async def test_it_is_saved_for_the_next_session(database: Database, session: AsyncSession) -> None:
    await consent(session)

    await set_auto_send(session, 3, provider="groq", version="2", clock=lambda: AT)

    async with database.session() as other:
        seen = await auto_send_permission(other, provider="groq", version="2")
    assert seen is not None and (seen.limit, seen.granted_at_utc) == (3, AT)


@pytest.mark.parametrize("limit", [-1, AUTO_SEND_LIMIT_MAX + 1, 50])
async def test_a_limit_outside_zero_to_ten_is_refused_and_changes_nothing(
    database: Database, session: AsyncSession, limit: int
) -> None:
    await consent(session)
    await set_auto_send(session, 2, provider="groq", version="2", clock=lambda: AT)

    with pytest.raises(ValueError, match="0 to 10"):
        await set_auto_send(session, limit, provider="groq", version="2", clock=lambda: LATER)

    async with database.session() as other:
        seen = await auto_send_permission(other, provider="groq", version="2")
    assert seen is not None and (seen.limit, seen.granted_at_utc) == (2, AT)


async def test_revoking_the_consent_ends_the_permission_at_once(session: AsyncSession) -> None:
    owner = await consent(session)
    await set_auto_send(session, 5, provider="groq", version="2", clock=lambda: AT)

    await ConsentRepository(session).revoke_all(owner.id, "groq", LATER)
    await session.commit()

    assert await auto_send_permission(session, provider="groq", version="2") is None
    with pytest.raises(ConfigurationError):
        await set_auto_send(session, 1, provider="groq", version="2")
    # Consenting again starts from none, never from the old permission.
    await ConsentRepository(session).grant(owner.id, "groq", "2", LATER)
    await session.commit()
    again = await auto_send_permission(session, provider="groq", version="2")
    assert again is not None and (again.limit, again.granted_at_utc) == (0, None)


async def test_a_new_disclosure_version_starts_without_the_permission(
    session: AsyncSession,
) -> None:
    await consent(session, version="2")
    await set_auto_send(session, 5, provider="groq", version="2", clock=lambda: AT)

    assert await auto_send_permission(session, provider="groq", version="3") is None
    with pytest.raises(ConfigurationError):
        await set_auto_send(session, 5, provider="groq", version="3")
    await consent(session, version="3")
    fresh = await auto_send_permission(session, provider="groq", version="3")
    assert fresh is not None and fresh.limit == 0


async def test_only_gmail_consents_to_this_provider_count(session: AsyncSession) -> None:
    await consent(session, "work", kind=ProviderKind.MICROSOFT)
    await consent(session, "me", provider="openai")

    assert await auto_send_permission(session, provider="groq", version="2") is None
    with pytest.raises(ConfigurationError):
        await set_auto_send(session, 3, provider="groq", version="2")


async def test_with_several_accounts_the_most_recent_consent_is_the_one(
    session: AsyncSession,
) -> None:
    await consent(session, "old", granted=AT)
    new = await consent(session, "new", granted=LATER)

    given = await set_auto_send(session, 2, provider="groq", version="2", clock=lambda: LATER)

    assert given.account_email == "new@example.com"
    consents = ConsentRepository(session)
    chosen = await consents.get_active(new.id, "groq", "2")
    assert chosen is not None and chosen.auto_send_limit == 2
    (older,) = [
        found
        for found in [
            await consents.get_active(account.id, "groq", "2")
            for account in await AccountRepository(session).list_all()
            if account.email_address == "old@example.com"
        ]
        if found is not None
    ]
    assert older.auto_send_limit == 0  # The other account's consent is untouched.


# The disclosure the permission is given under


def preview_for(limit: int) -> TransmissionPreview:
    return permission_preview(
        limit,
        provider_name="groq",
        model_name="test-model",
        body_character_limit=4_000,
        privacy_notice="Enable Zero Data Retention.",
    )


def test_the_preview_counts_the_messages_a_run_may_send() -> None:
    preview = preview_for(3)

    assert (preview.message_count, preview.first_use, preview.reused_count) == (3, False, 0)
    lines = disclosure_lines(preview)
    assert lines[0] == "MailBrief will send 3 messages to Groq (test-model) for analysis."
    assert "Your consent is remembered" not in " ".join(lines)  # It was given earlier.
    assert "Enable Zero Data Retention." in lines


def test_a_preview_needs_at_least_one_message() -> None:
    with pytest.raises(ValidationError):
        preview_for(0)


@pytest.mark.parametrize(
    ("limit", "sentence"),
    [
        (
            1,
            "Automatic runs may send up to 1 message from me@example.com to Groq without "
            "asking. Revoke consent in Settings to stop.",
        ),
        (
            7,
            "Automatic runs may send up to 7 messages from me@example.com to Groq without "
            "asking. Revoke consent in Settings to stop.",
        ),
    ],
)
def test_the_permission_sentence_says_how_many_from_which_account(
    limit: int, sentence: str
) -> None:
    assert permission_sentence(limit, "me@example.com", "groq") == sentence
