"""The automatic-analysis permission on the active consent (ADR 0017).

The desktop and the CLI read and change it here, through the same two functions. It belongs
to one account's active consent for one AI provider and disclosure version, so revoking the
consent, or a new disclosure version, ends it without anything else happening. Setting it
needs an active consent: the owner has then seen the disclosure and analyzed once with Sync
and review. When several Gmail accounts have an active consent, the most recently granted
one is the account meant. Nothing here logs an address.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.briefs import AutoSendPermission
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


async def auto_send_permission(
    session: AsyncSession, *, provider: str, version: str
) -> AutoSendPermission | None:
    """The permission on the active Gmail consent, 0 when none was given; None when no
    account has an active consent for this provider and disclosure version."""
    newest = await ConsentRepository(session).newest_active(
        provider, version, ProviderKind.GMAIL.value
    )
    return None if newest is None else _permission(*newest)


async def set_auto_send(
    session: AsyncSession,
    limit: int,
    *,
    provider: str,
    version: str,
    clock: Callable[[], datetime] = _utc_now,
) -> AutoSendPermission:
    """Allow automatic runs to send up to ``limit`` messages without asking; 0 turns it off.

    A limit above 0 records when it was given, and 0 clears both. Raises ValueError for a
    limit outside 0 to 10, and ConfigurationError, with a static message, when no account
    has an active consent. Commits once and rolls back on failure.
    """
    repository = ConsentRepository(session)
    newest = await repository.newest_active(provider, version, ProviderKind.GMAIL.value)
    if newest is None:
        raise ConfigurationError(NO_CONSENT)
    consent, account = newest
    try:
        await repository.set_auto_send(account.id, provider, version, limit, normalize_utc(clock()))
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    logger.info("Automatic-analysis permission set to %d messages", limit)
    return _permission(consent, account)
