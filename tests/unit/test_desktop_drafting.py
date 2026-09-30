"""The desktop runtime's AI drafting calls: real storage, Groq through respx, a fake Gmail."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from mailbrief.domain.bodies import BodySource, MessageBody
from mailbrief.domain.drafting import DraftContextPart, DraftingOptions, DraftingStatus
from mailbrief.domain.drafts import DraftVersionOrigin
from mailbrief.providers.groq.credentials import ENTRY, SERVICE
from mailbrief.ui import runtime as runtime_module
from mailbrief.ui.preferences import DesktopPreferences
from mailbrief.ui.runtime import DesktopRuntime
from tests.unit.providers.groq.groq_fixtures import (
    CHAT_URL,
    TEST_KEY,
    MemoryVault,
    output_text,
    response_body,
)
from tests.unit.test_desktop_actions import seed_brief

MARKER = "RUNTIME-BODY-MARKER-91c2"


class FakeGmail:
    """What gmail_provider would yield: only fetch_message_body is used by drafting."""

    opened = 0

    async def fetch_message_body(self, provider_message_id: str) -> MessageBody:
        return MessageBody(
            provider_message_id=provider_message_id,
            text=f"Could you approve the budget? {MARKER}",
            source=BodySource.PLAIN,
        )


@asynccontextmanager
async def fake_gmail(*_args: Any, **_kwargs: Any) -> AsyncIterator[FakeGmail]:
    FakeGmail.opened += 1
    yield FakeGmail()


@pytest.fixture(autouse=True)
def providers(monkeypatch: pytest.MonkeyPatch) -> MemoryVault:
    FakeGmail.opened = 0
    monkeypatch.setattr(runtime_module, "gmail_provider", fake_gmail)
    vault = MemoryVault({(SERVICE, ENTRY): TEST_KEY})
    monkeypatch.setattr("mailbrief.providers.groq.credentials.os_vault", lambda: vault)
    return vault


def stored_bytes(database: Path) -> bytes:
    """The database file and its journal files, as they are on disk."""
    return b"".join(
        item.read_bytes()
        for item in database.parent.iterdir()
        if item.name.startswith(database.name)
    )


class Approve:
    async def request_drafting_consent(self, preview: Any) -> bool:
        return True


def groq_answer(request: httpx.Request) -> httpx.Response:
    content = json.dumps(
        {"subject": None, "body": "Yes, approved. [[amount]]", "missing_context": ["amount"]}
    )
    return httpx.Response(200, json=response_body([output_text(content)]))


async def test_drafting_through_the_runtime(
    tmp_path: Path, respx_mock: respx.MockRouter, providers: MemoryVault
) -> None:
    path = tmp_path / "mailbrief.sqlite3"
    runtime = DesktopRuntime(path)
    route = respx_mock.post(CHAT_URL).mock(side_effect=groq_answer)
    try:
        assert await runtime.load_saved() is None
        assert not await runtime.drafting_ready()  # No model yet.
        await runtime.save_preferences(DesktopPreferences(groq_model="test-model"))
        assert await runtime.drafting_ready()
        assert "Drafting asks for consent first." in await runtime.ai_status()
        await seed_brief(path)
        saved = await runtime.load_saved()
        assert saved is not None
        reply = await runtime.create_reply_draft(saved.account_id, saved.items[0].message_key)

        parts = await runtime.available_drafting_parts(reply.public_id)
        assert parts == {DraftContextPart.SOURCE_EMAIL, DraftContextPart.CURRENT_TEXT}
        assert FakeGmail.opened == 0  # Nothing is downloaded to list the parts.

        text_only = DraftingOptions(parts=frozenset({DraftContextPart.CURRENT_TEXT}))
        await runtime.prepare_drafting(reply.public_id, text_only)
        assert FakeGmail.opened == 0  # The email wasn't chosen.

        with_email = DraftingOptions(parts=parts, instructions="Approve it.")
        plan = await runtime.prepare_drafting(reply.public_id, with_email)
        assert FakeGmail.opened == 1
        assert plan.request.source is not None and MARKER in plan.request.source.body

        outcome = await runtime.generate_draft(plan, Approve(), asyncio.Event())

        assert outcome.status is DraftingStatus.GENERATED
        assert outcome.draft is not None and outcome.draft.body == "Yes, approved. [[amount]]"
        sent = json.loads(json.loads(route.calls.last.request.content)["messages"][1]["content"])
        assert set(sent) == {"kind", "tone", "length", "instructions", "today", "source", "current"}
        assert "@" not in route.calls.last.request.content.decode()
        versions = await runtime.draft_versions(reply.public_id)
        assert versions[0].origin is DraftVersionOrigin.GENERATED
        assert "Drafting consent given." in await runtime.ai_status()

        assert await runtime.revoke_consent() == 1  # Drafting's owner consent.
        assert "Drafting asks for consent first." in await runtime.ai_status()
    finally:
        await runtime.close()

    assert MARKER.encode() not in stored_bytes(path)


async def test_no_key_means_drafting_is_not_offered(tmp_path: Path, providers: MemoryVault) -> None:
    providers.entries.clear()
    runtime = DesktopRuntime(tmp_path / "mailbrief.sqlite3")
    await runtime.save_preferences(DesktopPreferences(groq_model="test-model"))

    assert not await runtime.drafting_ready()
