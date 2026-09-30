"""Reading a thread's metadata for thread tracking: the request, sent mail and skipped items."""

from datetime import timedelta

import httpx
import pytest
import respx

from mailbrief.ports.errors import (
    AuthenticationRequiredError,
    MessageUnavailableError,
    ProviderResponseError,
)
from mailbrief.ports.threads import ThreadReader
from mailbrief.providers.gmail.client import MAX_RESPONSE_BYTES, THREADS_URL, GmailClient
from mailbrief.providers.gmail.mapper import map_metadata
from mailbrief.providers.gmail.provider import GmailProvider
from tests.unit.providers.gmail.metadata_fixtures import ACCOUNT, NOW, FakeSession, metadata

THREAD = "thread_a1"


def item(
    identifier: str, *labels: str, minutes: int = 0, thread: str = THREAD
) -> dict[str, object]:
    raw = metadata(identifier, received=NOW + timedelta(minutes=minutes))
    raw["threadId"] = thread
    raw["labelIds"] = list(labels)
    return raw


async def test_the_thread_request_asks_for_metadata_only(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(f"{THREADS_URL}/{THREAD}").respond(json={"id": THREAD, "messages": []})
    async with httpx.AsyncClient() as http:
        assert await GmailClient(http, FakeSession()).thread(THREAD) == {
            "id": THREAD,
            "messages": [],
        }
    request = route.calls[0].request
    assert request.method == "GET"
    assert request.url.params["format"] == "metadata"
    assert request.url.params.get_list("metadataHeaders") == ["From", "To", "Subject", "Message-ID"]
    assert request.url.params["fields"] == (
        "id,messages(id,threadId,labelIds,internalDate,snippet,payload/headers)"
    )
    assert "body" not in request.url.params["fields"]
    assert "raw" not in request.url.params["fields"]


async def test_a_missing_thread_is_none_and_a_bad_id_is_refused(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(f"{THREADS_URL}/gone").respond(404)
    async with httpx.AsyncClient() as http:
        client = GmailClient(http, FakeSession())
        assert await client.thread("gone") is None
        with pytest.raises(ProviderResponseError):
            await client.thread("../messages")


async def test_the_size_cap_applies_to_threads(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{THREADS_URL}/{THREAD}").respond(content=b"x" * (MAX_RESPONSE_BYTES + 1))
    async with httpx.AsyncClient() as http:
        with pytest.raises(ProviderResponseError, match="size limit"):
            await GmailClient(http, FakeSession()).thread(THREAD)


@pytest.mark.parametrize(("labels", "sent"), [(["SENT"], True), (["INBOX"], False), ([], False)])
def test_the_mapper_marks_sent_mail(labels: list[str], sent: bool) -> None:
    raw = metadata()
    raw["labelIds"] = labels
    message = map_metadata(raw, ACCOUNT)
    assert message.is_sent is sent
    assert message.is_in_inbox is ("INBOX" in labels)


async def connected(http: httpx.AsyncClient) -> GmailProvider:
    provider = GmailProvider(FakeSession(), GmailClient(http, FakeSession()))
    await provider.connect()
    return provider


async def test_a_thread_skips_drafts_trash_spam_strays_and_broken_items(
    respx_mock: respx.MockRouter,
) -> None:
    broken = item("broken", "INBOX")
    broken["internalDate"] = "yesterday"
    respx_mock.get(f"{THREADS_URL}/{THREAD}").respond(
        json={
            "id": THREAD,
            "messages": [
                item("reply", "INBOX", minutes=20),
                item("mine", "SENT", minutes=10),
                item("draft", "DRAFT", minutes=30),
                item("trash", "TRASH", "INBOX", minutes=31),
                item("spam", "SPAM", minutes=32),
                item("stray", "INBOX", minutes=33, thread="thread_other"),
                broken,
                "not an item",
                item("first", minutes=0),
            ],
        }
    )
    async with httpx.AsyncClient() as http:
        provider = await connected(http)
        assert isinstance(provider, ThreadReader)
        messages = await provider.fetch_thread(THREAD)

    assert [message.provider_message_id for message in messages] == ["first", "mine", "reply"]
    assert [message.is_sent for message in messages] == [False, True, False]
    assert [message.is_in_inbox for message in messages] == [False, False, True]
    assert {message.conversation_id for message in messages} == {THREAD}


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(404), MessageUnavailableError),
        (httpx.Response(200, json={"id": THREAD, "messages": "none"}), ProviderResponseError),
    ],
)
async def test_a_gone_or_invalid_thread_raises(
    respx_mock: respx.MockRouter, response: httpx.Response, error: type[Exception]
) -> None:
    respx_mock.get(f"{THREADS_URL}/{THREAD}").mock(return_value=response)
    async with httpx.AsyncClient() as http:
        provider = await connected(http)
        with pytest.raises(error):
            await provider.fetch_thread(THREAD)


async def test_a_thread_without_messages_is_empty(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{THREADS_URL}/{THREAD}").respond(json={"id": THREAD})
    async with httpx.AsyncClient() as http:
        assert await (await connected(http)).fetch_thread(THREAD) == ()


async def test_reading_a_thread_needs_a_connection(respx_mock: respx.MockRouter) -> None:
    async with httpx.AsyncClient() as http:
        provider = GmailProvider(FakeSession(), GmailClient(http, FakeSession()))
        with pytest.raises(AuthenticationRequiredError):
            await provider.fetch_thread(THREAD)
    assert not respx_mock.calls
