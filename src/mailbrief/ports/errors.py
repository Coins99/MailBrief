"""Provider-neutral errors surfaced to application services."""


class ProviderError(RuntimeError):
    """Base class for expected external-provider failures."""


class AuthenticationRequiredError(ProviderError):
    """The user must authenticate or reconnect before work can continue."""


class ProviderPermissionError(ProviderError):
    """The connected account did not grant the required permission."""


class ProviderRateLimitError(ProviderError):
    """The provider requested that MailBrief delay further requests."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ProviderResponseError(ProviderError):
    """The provider returned an invalid or unsupported response."""
