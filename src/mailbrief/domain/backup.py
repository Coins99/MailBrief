"""Versioned metadata for a portable MailBrief database backup."""

from datetime import timedelta

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

MAX_DATABASE_BYTES = 1024 * 1024 * 1024
MAX_METADATA_BYTES = 16 * 1024


class BackupMetadata(BaseModel):
    """An archive describes its snapshot, never credentials or file paths."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    format_version: int = Field(ge=1, le=1)
    created_at_utc: AwareDatetime
    schema_revisions: tuple[str, ...] = Field(min_length=1, max_length=1)
    database_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    credentials_included: bool

    @field_validator("created_at_utc")
    @classmethod
    def utc_only(cls, value: AwareDatetime) -> AwareDatetime:
        """Keep archive timestamps unambiguous."""
        if value.utcoffset() != timedelta(0):
            raise ValueError("The backup timestamp must be UTC.")
        return value

    @field_validator("credentials_included")
    @classmethod
    def no_credentials(cls, value: bool) -> bool:
        """Credential-bearing formats are never accepted."""
        if value:
            raise ValueError("Credentials are not supported in a backup.")
        return value
