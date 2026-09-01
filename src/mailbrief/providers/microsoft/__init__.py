"""Microsoft email provider package and adapter."""

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from mailbrief.domain.messages import AccountIdentity, MessagePage, ProviderKind
from mailbrief.ports.email_provider import EmailProvider
from mailbrief.ports.errors import AuthenticationRequiredError
from mailbrief.providers.microsoft.auth import MicrosoftAuth, get_default_token_cache
from mailbrief.providers.microsoft.graph_client import GraphClient
from mailbrief.providers.microsoft.mapper import (
    map_account_identity,
    map_message_page,
)

__all__ = [
    "DEFAULT_AUTHORITY",
    "DEFAULT_SCOPES",
    "GraphClient",
    "MicrosoftAuth",
    "MicrosoftEmailProvider",
    "get_default_token_cache",
]


class MicrosoftEmailProvider(EmailProvider):
    """Adapter bridging Microsoft Graph to MailBrief's EmailProvider protocol."""

    def __init__(self, auth: MicrosoftAuth, graph_client: GraphClient) -> None:
        self._auth = auth
        self._client = graph_client
        self._account: AccountIdentity | None = None

    @property
    def provider_kind(self) -> ProviderKind:
        return ProviderKind.MICROSOFT

    async def connect(self) -> AccountIdentity:
        """Connect interactively or silently restore session."""
        try:
            await self._auth.get_access_token()
        except AuthenticationRequiredError:
            await self._auth.acquire_token_interactive()

        account_data = await self._client.get("me")
        self._account = map_account_identity(account_data)
        return self._account

    async def current_account(self) -> AccountIdentity | None:
        """Return the connected account or None."""
        if self._account:
            return self._account
        try:
            account_data = await self._client.get("me")
            self._account = map_account_identity(account_data)
            return self._account
        except AuthenticationRequiredError:
            return None

    async def iter_message_pages(
        self,
        *,
        range_start_utc: datetime,
        range_end_utc: datetime,
    ) -> AsyncIterator[MessagePage]:
        """Paginate Inbox messages received within the specified UTC boundaries."""
        account = await self.current_account()
        if not account:
            raise AuthenticationRequiredError("Must connect before retrieving messages.")

        start_iso = range_start_utc.isoformat().replace("+00:00", "Z")
        end_iso = range_end_utc.isoformat().replace("+00:00", "Z")

        params: dict[str, Any] | None = {
            "$select": (
                "id,internetMessageId,conversationId,subject,sender,toRecipients,"
                "receivedDateTime,isRead,importance,hasAttachments,bodyPreview,webLink"
            ),
            "$filter": f"receivedDateTime ge {start_iso} and receivedDateTime lt {end_iso}",
            "$orderby": "receivedDateTime desc",
            "$top": 50,
        }

        url = "me/mailFolders/inbox/messages"
        page_number = 1

        while url:
            data = await self._client.get(url, params=params)
            page = map_message_page(data, account.provider_account_id, page_number)
            yield page

            url = page.continuation or ""
            params = None  # Continuation URLs contain parameters embedded by Graph
            page_number += 1

    async def fetch_plain_text_body(self, provider_message_id: str) -> str:
        """Fetch plain-text body for one shortlisted message."""
        headers = {"Prefer": 'outlook.body-content-type="text"'}
        params = {"$select": "id,body"}
        data = await self._client.get(
            f"me/messages/{provider_message_id}",
            params=params,
            headers=headers,
        )
        body = data.get("body", {})
        return str(body.get("content", ""))

    async def disconnect(self) -> None:
        """Clear local session and cached tokens."""
        self._account = None
        await self._auth.disconnect()
