"""Microsoft Graph implementation of the email-provider port (dormant)."""

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from urllib.parse import quote

from mailbrief.domain.messages import AccountIdentity, MessagePage, ProviderKind
from mailbrief.ports.email_provider import EmailProvider
from mailbrief.ports.errors import (
    AuthenticationRequiredError,
    ProviderError,
    ProviderResponseError,
)
from mailbrief.providers.microsoft.auth import MicrosoftAuth
from mailbrief.providers.microsoft.graph_client import GraphClient
from mailbrief.providers.microsoft.mapper import map_account_identity, map_message_page


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

        cached = await self._auth.get_account_identity_from_cache()
        if cached is None:
            raise ProviderError("Malformed Microsoft account record in token cache.")

        account_data = await self._client.get("me")
        self._account = map_account_identity(
            account_data,
            provider_account_id=cached.provider_account_id,
            tenant_id=cached.tenant_id,
            extra_addresses=(cached.email_address,),
        )
        return self._account

    async def current_account(self) -> AccountIdentity | None:
        """Return the connected account or None."""
        if self._account:
            return self._account
        cached = await self._auth.get_account_identity_from_cache()
        if cached:
            self._account = cached
            return self._account
        try:
            account_data = await self._client.get("me")
            self._account = map_account_identity(account_data)
            return self._account
        except (AuthenticationRequiredError, ProviderError):
            return None

    async def iter_message_pages(
        self,
        *,
        range_start_utc: datetime,
        range_end_utc: datetime,
        continuation: str | None = None,
        max_pages: int = 200,
    ) -> AsyncIterator[MessagePage]:
        """Paginate Inbox messages received within the specified UTC boundaries."""
        account = self._account or await self._auth.get_account_identity_from_cache()
        if not account:
            account = await self.current_account()
        if not account:
            raise AuthenticationRequiredError("Must connect before retrieving messages.")

        start_iso = range_start_utc.isoformat().replace("+00:00", "Z")
        end_iso = range_end_utc.isoformat().replace("+00:00", "Z")

        params: dict[str, Any] | None = (
            None
            if continuation
            else {
                "$select": (
                    "id,internetMessageId,conversationId,subject,sender,toRecipients,"
                    "receivedDateTime,isRead,importance,hasAttachments,bodyPreview,webLink"
                ),
                "$filter": f"receivedDateTime ge {start_iso} and receivedDateTime lt {end_iso}",
                "$orderby": "receivedDateTime desc",
                "$top": 50,
            }
        )

        url = continuation or "me/mailFolders/inbox/messages"
        page_number = 1
        seen_urls: set[str] = set()

        while url:
            if page_number > max_pages:
                raise ProviderResponseError(
                    f"Maximum pagination page limit ({max_pages}) exceeded."
                )  # noqa: E501

            if url in seen_urls:
                raise ProviderResponseError("Circular continuation link detected in pagination.")
            seen_urls.add(url)

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
        encoded_message_id = quote(provider_message_id, safe="")
        data = await self._client.get(
            f"me/messages/{encoded_message_id}",
            params=params,
            headers=headers,
        )
        body = data.get("body")
        if not isinstance(body, dict):
            raise ProviderResponseError("Microsoft Graph returned a malformed message body.")
        content_type = body.get("contentType")
        content = body.get("content")
        if (
            not isinstance(content_type, str)
            or content_type.casefold() != "text"
            or not isinstance(content, str)
        ):
            raise ProviderResponseError("Microsoft Graph returned a malformed message body.")
        return content

    async def disconnect(self) -> None:
        """Clear local session and cached tokens."""
        self._account = None
        await self._auth.disconnect()
