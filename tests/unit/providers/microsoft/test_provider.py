"""Unit tests for the MicrosoftEmailProvider adapter."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import msal
import pytest

from mailbrief.domain.messages import MessagePage, ProviderKind
from mailbrief.ports.email_provider import EmailProvider
from mailbrief.ports.errors import (
    AuthenticationRequiredError,
    ProviderError,
    ProviderResponseError,
)
from mailbrief.providers.microsoft import MicrosoftEmailProvider
from mailbrief.providers.microsoft.auth import MicrosoftAuth
from mailbrief.providers.microsoft.graph_client import GraphClient


@pytest.fixture
def mock_auth() -> MagicMock:
    auth = MagicMock(spec=MicrosoftAuth)
    auth.get_access_token = AsyncMock(return_value="valid-token")
    auth.acquire_token_interactive = AsyncMock(return_value=None)
    auth.disconnect = AsyncMock()
    return auth


@pytest.fixture
def mock_graph_client() -> MagicMock:
    client = MagicMock(spec=GraphClient)
    client.get = AsyncMock()
    return client


def test_microsoft_provider_satisfies_protocol(
    mock_auth: MagicMock,
    mock_graph_client: MagicMock,
) -> None:
    provider = MicrosoftEmailProvider(mock_auth, mock_graph_client)
    assert isinstance(provider, EmailProvider)
    assert provider.provider_kind is ProviderKind.MICROSOFT


async def test_connect_success(
    mock_auth: MagicMock,
    mock_graph_client: MagicMock,
) -> None:
    provider = MicrosoftEmailProvider(mock_auth, mock_graph_client)
    mock_graph_client.get.return_value = {
        "id": "acc-1",
        "mail": "taylor@example.com",
        "displayName": "Taylor Smith",
    }

    account = await provider.connect()

    assert account.provider is ProviderKind.MICROSOFT
    assert account.provider_account_id == "acc-1"
    assert account.email_address == "taylor@example.com"
    assert account.display_name == "Taylor Smith"


async def test_connect_interactive_fallback(
    mock_auth: MagicMock,
    mock_graph_client: MagicMock,
) -> None:
    provider = MicrosoftEmailProvider(mock_auth, mock_graph_client)
    mock_auth.get_access_token.side_effect = [
        AuthenticationRequiredError("No session"),
        "new-token",
    ]
    mock_graph_client.get.return_value = {
        "id": "acc-1",
        "mail": "taylor@example.com",
    }

    account = await provider.connect()

    mock_auth.acquire_token_interactive.assert_awaited_once()
    assert account.provider_account_id == "acc-1"


async def test_connect_recovers_interactively_when_silent_msal_returns_none(
    mock_graph_client: MagicMock,
) -> None:
    application = MagicMock()
    account = {"home_account_id": "uid.tenant", "username": "user@example.com"}
    application.get_accounts.return_value = [account]
    application.acquire_token_silent_with_error.return_value = None
    application.acquire_token_interactive.return_value = {
        "access_token": "interactive-token",
        "id_token_claims": {"preferred_username": "user@example.com"},
    }
    auth = MicrosoftAuth(
        "client-id",
        token_cache=msal.SerializableTokenCache(),
        _application=application,
    )
    mock_graph_client.get.return_value = {"id": "account", "mail": "user@example.com"}

    connected = await MicrosoftEmailProvider(auth, mock_graph_client).connect()
    assert connected.email_address == "user@example.com"
    application.acquire_token_interactive.assert_called_once()


async def test_connect_does_not_open_browser_for_provider_outage(
    mock_auth: MagicMock,
    mock_graph_client: MagicMock,
) -> None:
    provider = MicrosoftEmailProvider(mock_auth, mock_graph_client)
    mock_auth.get_access_token.side_effect = ProviderError("service unavailable")

    with pytest.raises(ProviderError):
        await provider.connect()
    mock_auth.acquire_token_interactive.assert_not_awaited()


async def test_current_account_returns_cached_or_fetches(
    mock_auth: MagicMock,
    mock_graph_client: MagicMock,
) -> None:
    provider = MicrosoftEmailProvider(mock_auth, mock_graph_client)
    mock_graph_client.get.return_value = {
        "id": "acc-1",
        "mail": "taylor@example.com",
    }

    account = await provider.current_account()
    assert account is not None
    assert account.provider_account_id == "acc-1"

    # Second call returns cached account without fetching again
    cached = await provider.current_account()
    assert cached == account
    assert mock_graph_client.get.call_count == 1


async def test_iter_message_pages(
    mock_auth: MagicMock,
    mock_graph_client: MagicMock,
) -> None:
    provider = MicrosoftEmailProvider(mock_auth, mock_graph_client)
    mock_graph_client.get.side_effect = [
        # Call for current_account
        {"id": "acc-1", "mail": "taylor@example.com"},
        # Page 1
        {
            "value": [
                {
                    "id": "msg-1",
                    "receivedDateTime": "2026-08-31T12:00:00Z",
                    "sender": {"emailAddress": {"name": "A", "address": "a@example.com"}},
                    "toRecipients": [],
                    "webLink": "https://outlook.office.com/mail/id/msg-1",
                }
            ],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages?$skip=50",
        },
        # Page 2
        {
            "value": [
                {
                    "id": "msg-2",
                    "receivedDateTime": "2026-08-31T13:00:00Z",
                    "sender": {"emailAddress": {"name": "B", "address": "b@example.com"}},
                    "toRecipients": [],
                    "webLink": "https://outlook.office.com/mail/id/msg-2",
                }
            ],
        },
    ]

    pages: list[MessagePage] = []
    async for page in provider.iter_message_pages(
        range_start_utc=datetime(2026, 8, 31, 0, 0, tzinfo=UTC),
        range_end_utc=datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
    ):
        pages.append(page)

    assert len(pages) == 2
    assert pages[0].page_number == 1
    assert pages[0].messages[0].provider_message_id == "msg-1"
    assert pages[1].page_number == 2
    assert pages[1].messages[0].provider_message_id == "msg-2"


async def test_fetch_plain_text_body(
    mock_auth: MagicMock,
    mock_graph_client: MagicMock,
) -> None:
    provider = MicrosoftEmailProvider(mock_auth, mock_graph_client)
    mock_graph_client.get.return_value = {
        "id": "msg-1",
        "body": {"contentType": "text", "content": "Hello World body"},
    }

    body = await provider.fetch_plain_text_body("msg-1")

    assert body == "Hello World body"
    mock_graph_client.get.assert_awaited_once_with(
        "me/messages/msg-1",
        params={"$select": "id,body"},
        headers={"Prefer": 'outlook.body-content-type="text"'},
    )


async def test_fetch_plain_text_body_encodes_id_and_rejects_invalid_body(
    mock_auth: MagicMock,
    mock_graph_client: MagicMock,
) -> None:
    provider = MicrosoftEmailProvider(mock_auth, mock_graph_client)
    mock_graph_client.get.return_value = {"body": {"contentType": "html", "content": "private"}}

    with pytest.raises(ProviderResponseError):
        await provider.fetch_plain_text_body("folder/id ?")
    assert mock_graph_client.get.await_args.args[0] == "me/messages/folder%2Fid%20%3F"


async def test_disconnect(
    mock_auth: MagicMock,
    mock_graph_client: MagicMock,
) -> None:
    provider = MicrosoftEmailProvider(mock_auth, mock_graph_client)
    mock_graph_client.get.return_value = {
        "id": "acc-1",
        "mail": "taylor@example.com",
    }
    await provider.current_account()

    await provider.disconnect()

    mock_auth.disconnect.assert_awaited_once()
    mock_graph_client.get.side_effect = AuthenticationRequiredError("No account")
    assert await provider.current_account() is None
