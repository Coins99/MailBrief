"""Safe, application-authored Gmail setup errors suitable for diagnostic output."""

import logging

from mailbrief.errors import ConfigurationError
from mailbrief.ports.errors import ProviderResponseError

logger = logging.getLogger(__name__)


def response_error(message: str) -> ProviderResponseError:
    """Log application-authored guidance before sync reduces the error to an audit code.

    Callers must use only static messages and numeric HTTP statuses, never server text,
    URLs, identifiers, credentials or exception strings.
    """
    logger.warning("Gmail diagnostic: %s", message)
    return ProviderResponseError(message)


class GmailSetupError(ConfigurationError):
    """Use only static guidance: never include paths, payloads or backend exceptions."""


def permission_guidance(payload: object) -> str:
    """Translate recognized Google reasons without exposing response contents."""
    reasons: set[str] = set()
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        for field in ("errors", "details"):
            entries = error.get(field)
            if isinstance(entries, list):
                for entry in entries:
                    reason = entry.get("reason") if isinstance(entry, dict) else None
                    if isinstance(reason, str):
                        reasons.add(reason)
    if reasons & {"accessNotConfigured", "SERVICE_DISABLED"}:
        return (
            "Gmail API is disabled for this OAuth client's Google Cloud project. "
            "Enable Gmail API in that project's API Library, then retry."
        )
    if reasons & {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}:
        return (
            "Google did not grant the required Gmail permission. "
            "Reconnect interactively and approve Gmail read-only access."
        )
    if reasons & {"domainPolicy", "ORG_RESTRICTION_VIOLATION"}:
        return "Your Google Workspace organization blocks this access; contact its administrator."
    if reasons & {"rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded"}:
        return (
            "Google rejected the request because of an API quota limit; "
            "check quotas and retry later."
        )
    return (
        "Gmail access denied. Check that Gmail API is enabled in the OAuth client's "
        "project and that read-only consent was granted."
    )
