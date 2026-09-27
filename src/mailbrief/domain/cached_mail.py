"""Provider-independent snapshots for read-only, offline metadata browsing."""

from datetime import date, datetime

from pydantic import Field, field_validator

from mailbrief.domain.common import DomainModel, normalize_utc
from mailbrief.domain.messages import NormalizedMessage


class CachedAccount(DomainModel):
    account_id: int = Field(ge=1)
    email_address: str
    last_sync_at_utc: datetime | None = None

    @field_validator("last_sync_at_utc")
    @classmethod
    def normalize_sync(cls, value: datetime | None) -> datetime | None:
        return normalize_utc(value) if value is not None else None


class CachedMailPage(DomainModel):
    account: CachedAccount
    local_date: date
    timezone_name: str
    messages: tuple[NormalizedMessage, ...] = ()
    offset: int = Field(default=0, ge=0)
    has_more: bool = False
