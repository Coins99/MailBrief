"""The transaction base of the services that change the owner's records."""

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from mailbrief.domain.common import normalize_utc, utc_now


def new_public_id() -> str:
    return str(uuid.uuid4())


class OwnedRecordService:
    """Shared by the actions, drafts and proposals services: every write reads the clock
    once, commits once and rolls back on failure.

    A write over a record another writer changed after this session loaded it fails with
    the service's conflict error (_stale_error), across sessions and processes.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        clock: Callable[[], datetime] = utc_now,
        id_factory: Callable[[], str] = new_public_id,
    ) -> None:
        self._session = session
        self._clock = clock
        self._id_factory = id_factory

    def _stale_error(self) -> Exception:
        raise NotImplementedError

    async def _write[T](self, operation: Callable[[datetime], Awaitable[T]]) -> T:
        now = normalize_utc(self._clock())
        try:
            result = await operation(now)
            await self._session.commit()
        except StaleDataError:
            await self._session.rollback()
            raise self._stale_error() from None
        except BaseException:
            await self._session.rollback()
            raise
        return result
