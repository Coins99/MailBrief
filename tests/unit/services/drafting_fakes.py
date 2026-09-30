"""Scriptable drafting provider, body reader and consent gate for drafting tests."""

from collections.abc import Callable, Sequence
from typing import ClassVar

from mailbrief.domain.bodies import BodySource, MessageBody
from mailbrief.domain.drafting import (
    DraftCandidate,
    DraftingPreview,
    DraftingRequest,
    DraftingResponse,
)
from mailbrief.ports.errors import MessageUnavailableError

DraftScriptItem = DraftingResponse | Exception | Callable[[DraftingRequest], DraftingResponse]


def answer(
    body: str = "Hi Alex,\n\nYes, I can do that. [[date]]",
    subject: str | None = None,
    missing: Sequence[str] = ("the delivery date",),
) -> DraftingResponse:
    return DraftingResponse(
        candidate=DraftCandidate(subject=subject, body=body, missing_context=tuple(missing))
    )


class FakeDraftingProvider:
    """DraftingProvider fake: one script item per draft() call; records every request."""

    created: ClassVar[list["FakeDraftingProvider"]] = []

    def __init__(self, script: Sequence[DraftScriptItem] = (), *, credentials: bool = True) -> None:
        self.script = list(script)
        self.requests: list[DraftingRequest] = []
        self.credentials = credentials
        FakeDraftingProvider.created.append(self)

    @property
    def provider_name(self) -> str:
        return "groq"

    @property
    def model_name(self) -> str:
        return "fake-model"

    @property
    def drafting_prompt_version(self) -> str:
        return "fake-draft-1"

    @property
    def privacy_notice(self) -> str:
        return "The fake provider keeps nothing."

    @property
    def requests_sent(self) -> int:
        return len(self.requests)

    async def credentials_available(self) -> bool:
        return self.credentials

    async def draft(self, request: DraftingRequest) -> DraftingResponse:
        self.requests.append(request)
        if not self.script:
            raise AssertionError("unexpected draft() call")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, DraftingResponse):
            return item
        return item(request)


class FakeBodies:
    """A body reader that records each download and never touches the network."""

    def __init__(self, text: str = "", *, missing: bool = False) -> None:
        self.text = text
        self.missing = missing
        self.fetched: list[str] = []

    async def fetch_message_body(self, provider_message_id: str) -> MessageBody:
        self.fetched.append(provider_message_id)
        if self.missing:
            raise MessageUnavailableError("gone")
        return MessageBody(
            provider_message_id=provider_message_id,
            text=self.text,
            source=BodySource.PLAIN if self.text else BodySource.NONE,
        )


class Gate:
    """Records every preview it is shown and answers with ``approve``."""

    def __init__(self, approve: bool = True) -> None:
        self.approve = approve
        self.previews: list[DraftingPreview] = []

    async def request_drafting_consent(self, preview: DraftingPreview) -> bool:
        self.previews.append(preview)
        return self.approve
