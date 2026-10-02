"""The automatic-analysis permission on the active consent (ADR 0017).

The desktop and the CLI read and change it here, through the same two functions. It belongs
to one account's active consent for one AI provider and disclosure version, so revoking the
consent, or a new disclosure version, ends it without anything else happening. Setting it
needs an active consent: the owner has then seen the disclosure and analyzed once with Sync
and review. The account is always the connected Gmail account, the one a brief reads its
consent for, and at most one account holds a permission: giving it to one clears every
other's, and turning it off clears them all. Nothing here logs an address.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.briefs import AUTO_SEND_LIMIT_MAX, AutoSendPermission
from mailbrief.domain.common import normalize_utc
from mailbrief.domain.messages import ProviderKind
from mailbrief.errors import ConfigurationError
from mailbrief.storage.repositories import ConsentRepository
from mailbrief.storage.tables import AccountTable, AIConsentTable

logger = logging.getLogger(__name__)

NO_CONSENT: Final = "Analyze once with Sync and review to give consent first."


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _permission(consent: AIConsentTable, account: AccountTable) -> AutoSendPermission:
    return AutoSendPermission(
        account_email=account.email_address,
        limit=consent.auto_send_limit,
        granted_at_utc=consent.auto_send_granted_at_utc,
    )


async def _active(
    session: AsyncSession, account_email: str | None, provider: str, version: str
) -> tuple[AIConsentTable, AccountTable] | None:
    """The Gmail account with this address and its active consent; None without either."""
    if account_email is None:
        return None
    account = await session.scalar(
        select(AccountTable)
        .where(
            AccountTable.provider == ProviderKind.GMAIL.value,
            AccountTable.email_address == account_email,
        )
        .execution_options(populate_existing=True)
    )
    if account is None:
        return None
    consent = await ConsentRepository(session).get_active(account.id, provider, version)
    return None if consent is None else (consent, account)


async def auto_send_permission(
    session: AsyncSession, account_email: str | None, *, provider: str, version: str
) -> AutoSendPermission | None:
    """The permission on the connected Gmail account's active consent, 0 when none was
    given; None when that account has no active consent for this provider and disclosure
    version, or when no account is connected (``account_email`` None)."""
    found = await _active(session, account_email, provider, version)
    return None if found is None else _permission(*found)


async def set_auto_send(
    session: AsyncSession,
    limit: int,
    account_email: str | None,
    *,
    provider: str,
    version: str,
    clock: Callable[[], datetime] = _utc_now,
) -> AutoSendPermission | None:
    """Allow automatic runs to send up to ``limit`` of the connected account's messages
    without asking; 0 turns it off for every account.

    A limit above 0 needs the connected account's active consent (ConfigurationError, with a
    static message, without one). It records when it was given and clears every other
    account's permission, so at most one account holds one. 0 clears every account's and
    needs neither an account nor a consent. Returns the connected account's permission, or
    None when it has no active consent. Raises ValueError for a limit outside 0 to 10.
    Commits once and rolls back on failure.
    """
    if not 0 <= limit <= AUTO_SEND_LIMIT_MAX:
        raise ValueError(f"The limit must be 0 to {AUTO_SEND_LIMIT_MAX}.")
    repository = ConsentRepository(session)
    found = await _active(session, account_email, provider, version)
    if limit and found is None:
        raise ConfigurationError(NO_CONSENT)
    try:
        for other in await repository.with_auto_send(provider):
            if found is None or other.id != found[0].id:
                other.auto_send_limit = 0
                other.auto_send_granted_at_utc = None
        if found is not None:
            await repository.set_auto_send(
                found[1].id, provider, version, limit, normalize_utc(clock())
            )
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    logger.info("Automatic-analysis permission set to %d messages", limit)
    return None if found is None else _permission(*found)
