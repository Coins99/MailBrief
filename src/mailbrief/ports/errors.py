"""Provider-neutral errors surfaced to application services."""

from typing import Final

# A MailBrief-assigned provider_error_code, not one the provider sends: the provider refused
# the caller's network (for example a VPN, proxy or data centre) before checking credentials.
NETWORK_BLOCKED_CODE: Final = "network_blocked"


class ProviderError(RuntimeError):
    """Base class for expected external-provider failures."""

    def __init__(
        self,
        message: str,
        *,
        client_request_id: str | None = None,
        provider_error_code: str | None = None,
        server_request_id: str | None = None,
        accumulated_sleep_seconds: float = 0.0,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.client_request_id = client_request_id
        self.provider_error_code = provider_error_code
        self.server_request_id = server_request_id
        self.accumulated_sleep_seconds = accumulated_sleep_seconds
        self.http_status = http_status  # The HTTP status behind the failure, when there was one.


class ProviderTimeoutError(ProviderError):
    """An external provider operation exceeded its wall-clock timeout deadline."""


class ProviderUsageLimitError(ProviderError):
    """The local usage budget is exhausted; do not retry this connection."""


class AuthenticationRequiredError(ProviderError):
    """The user must authenticate or reconnect before work can continue."""


class AIAuthenticationError(ProviderError):
    """The AI provider rejected credentials or configuration (e.g. invalid API key)."""


class AICredentialsMissingError(AIAuthenticationError):
    """No usable AI API key is available, so nothing was sent."""


class AuthenticationCancelledError(AuthenticationRequiredError):
    """The user cancelled or denied an interactive authentication prompt."""


class ProviderPermissionError(ProviderError):
    """The connected account did not grant the required permission."""


class ProviderRateLimitError(ProviderError):
    """The provider requested that MailBrief delay further requests."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None = None,
        client_request_id: str | None = None,
        provider_error_code: str | None = None,
        server_request_id: str | None = None,
        accumulated_sleep_seconds: float = 0.0,
        http_status: int | None = None,
    ) -> None:
        super().__init__(
            message,
            client_request_id=client_request_id,
            provider_error_code=provider_error_code,
            server_request_id=server_request_id,
            accumulated_sleep_seconds=accumulated_sleep_seconds,
            http_status=http_status,
        )
        self.retry_after_seconds = retry_after_seconds


class ProviderResponseError(ProviderError):
    """The provider returned an invalid or unsupported response."""


class MessageUnavailableError(ProviderResponseError):
    """The message no longer exists or can no longer be read, for example after deletion."""


class ProviderRequestRejectedError(ProviderResponseError):
    """The provider refused this particular request; other requests may still succeed."""


class ProviderUnavailableError(ProviderResponseError):
    """The provider failed, or answered unreadably, after retries."""
