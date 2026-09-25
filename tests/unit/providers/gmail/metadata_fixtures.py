"""Synthetic metadata and session fixture; never uses real credentials."""

from datetime import UTC, datetime

from mailbrief.domain.messages import AccountIdentity, ProviderKind

NOW = datetime(2026, 9, 25, 14, tzinfo=UTC)
ACCOUNT = AccountIdentity(
    provider=ProviderKind.GMAIL,
    provider_account_id="me@example.com",
    email_address="me@example.com",
)


def metadata(
    identifier: str = "a1", *, received: datetime = NOW, inbox: bool = True
) -> dict[str, object]:
    return {
        "id": identifier,
        "threadId": "thread_" + identifier,
        "internalDate": str(int(received.timestamp() * 1000)),
        "labelIds": ["UNREAD", "IMPORTANT", *(["INBOX"] if inbox else [])],
        "snippet": "Please approve &amp; reply.",
        "payload": {
            "headers": [
                {"name": "From", "value": "Sender <sender@example.com>"},
                {"name": "To", "value": "Me <me@example.com>"},
                {"name": "Subject", "value": "Approval needed"},
                {"name": "Message-ID", "value": "<synthetic@example.com>"},
            ]
        },
    }


class FakeSession:
    def __init__(self) -> None:
        self.invalidated: list[str] = []
        self.connected = False

    async def connect(self, *, silent_only: bool = False) -> AccountIdentity:
        self.connected = True
        return ACCOUNT

    async def disconnect(self) -> None:
        self.connected = False

    async def access_token(self) -> str:
        return "fake-token"

    async def invalidate_access_token(self, rejected_token: str) -> None:
        self.invalidated.append(rejected_token)


async def no_sleep(seconds: float) -> None:
    pass
