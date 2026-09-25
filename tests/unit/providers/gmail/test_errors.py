"""Permission guidance stays actionable without printing Google's raw payload."""

import httpx
import pytest

from mailbrief.ports.errors import ProviderPermissionError
from mailbrief.providers.gmail.auth import GmailAuth
from mailbrief.providers.gmail.errors import permission_guidance


@pytest.mark.parametrize("field", ["errors", "details"])
@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("accessNotConfigured", "Enable Gmail API"),
        ("SERVICE_DISABLED", "Enable Gmail API"),
        ("insufficientPermissions", "Reconnect interactively"),
        ("ACCESS_TOKEN_SCOPE_INSUFFICIENT", "Reconnect interactively"),
        ("domainPolicy", "administrator"),
        ("ORG_RESTRICTION_VIOLATION", "administrator"),
        ("dailyLimitExceeded", "quota limit"),
        ("private-unknown-reason", "Gmail access denied"),
    ],
)
def test_permission_response_is_safe(field: str, reason: str, expected: str) -> None:
    response = httpx.Response(
        403,
        json={"error": {"message": "private-token", field: [{"reason": reason}]}},
    )
    with pytest.raises(ProviderPermissionError) as caught:
        GmailAuth._check_status(response)
    assert expected in str(caught.value)
    assert "private" not in str(caught.value)


@pytest.mark.parametrize(
    "payload",
    [None, [], {"error": "private"}, {"error": {"details": [None, {"reason": []}]}}],
)
def test_malformed_permission_payload(payload: object) -> None:
    assert permission_guidance(payload).startswith("Gmail access denied")


def test_non_json_permission_response() -> None:
    with pytest.raises(ProviderPermissionError, match="Gmail access denied"):
        GmailAuth._check_status(httpx.Response(403, text="private-token"))
