"""Gmail pagination, exact local-day windows, concurrency and cancellation."""

import asyncio
from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from mailbrief.ports.email_provider import EmailProvider
from mailbrief.ports.errors import AuthenticationRequiredError, ProviderResponseError
from mailbrief.providers.gmail.client import MESSAGES_URL, GmailClient
from mailbrief.providers.gmail.provider import GmailProvider
from mailbrief.services.calendar import local_day_window
from tests.unit.providers.gmail.metadata_fixtures import NOW, FakeSession, metadata, no_sleep


async def test_pages_dedup_boundaries_and_disappearance(respx_mock: respx.MockRouter) -> None:
    window = local_day_window(NOW, ZoneInfo("America/Toronto"))
    listing = respx_mock.get(MESSAGES_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "messages": [{"id": i} for i in ("a", "b", "gone")],
                    "nextPageToken": "page2",
                },
            ),
            httpx.Response(200, json={"messages": [{"id": i} for i in ("a", "c", "archived")]}),
        ]
    )
    respx_mock.get(MESSAGES_URL + "/a").respond(json=metadata("a", received=window.start_utc))
    respx_mock.get(MESSAGES_URL + "/b").respond(json=metadata("b", received=window.end_utc))
    respx_mock.get(MESSAGES_URL + "/c").respond(
        json=metadata("c", received=window.start_utc - timedelta(milliseconds=1))
    )
    respx_mock.get(MESSAGES_URL + "/gone").respond(404)
    respx_mock.get(MESSAGES_URL + "/archived").respond(json=metadata("archived", inbox=False))
    async with httpx.AsyncClient() as http:
        provider = GmailProvider(FakeSession(), GmailClient(http, FakeSession()))
        assert isinstance(provider, EmailProvider)
        await provider.connect()
        pages = [
            p
            async for p in provider.iter_message_pages(
                range_start_utc=window.start_utc, range_end_utc=window.end_utc
            )
        ]
    assert [[m.provider_message_id for m in p.messages] for p in pages] == [["a"], []]
    assert not any(p.failed_message_count for p in pages)
    assert listing.calls[1].request.url.params["pageToken"] == "page2"
    assert listing.calls[0].request.url.params["labelIds"] == "INBOX"
    assert listing.calls[0].request.url.params["q"] == (
        f"after:{int(window.start_utc.timestamp()) - 1} "
        f"before:{int(window.end_utc.timestamp()) + 1}"
    )


async def test_item_failure_preserves_successful_items(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(MESSAGES_URL).respond(json={"messages": [{"id": "good"}, {"id": "bad"}]})
    respx_mock.get(MESSAGES_URL + "/good").respond(json=metadata("good"))
    respx_mock.get(MESSAGES_URL + "/bad").respond(json={})
    async with httpx.AsyncClient() as http:
        provider = GmailProvider(FakeSession(), GmailClient(http, FakeSession(), sleep=no_sleep))
        await provider.connect()
        pages = [
            p
            async for p in provider.iter_message_pages(
                range_start_utc=NOW - timedelta(hours=1), range_end_utc=NOW + timedelta(hours=1)
            )
        ]
    assert pages[0].failed_message_count == 1
    assert pages[0].messages[0].provider_message_id == "good"


async def test_pagination_cycle_is_failure(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(MESSAGES_URL).respond(json={"nextPageToken": "same"})
    async with httpx.AsyncClient() as http:
        provider = GmailProvider(FakeSession(), GmailClient(http, FakeSession()))
        await provider.connect()
        with pytest.raises(ProviderResponseError, match="repeated"):
            _ = [
                p
                async for p in provider.iter_message_pages(
                    range_start_utc=NOW, range_end_utc=NOW + timedelta(days=1)
                )
            ]


async def test_session_and_body_boundary() -> None:
    async with httpx.AsyncClient() as http:
        provider = GmailProvider(FakeSession(), GmailClient(http, FakeSession()))
        assert await provider.current_account() is None
        with pytest.raises(AuthenticationRequiredError):
            _ = [
                p async for p in provider.iter_message_pages(range_start_utc=NOW, range_end_utc=NOW)
            ]
        await provider.connect()
        with pytest.raises(ValueError):
            _ = [
                p async for p in provider.iter_message_pages(range_start_utc=NOW, range_end_utc=NOW)
            ]
        with pytest.raises(ProviderResponseError, match="M3"):
            await provider.fetch_plain_text_body("a")
        await provider.disconnect()
        assert await provider.current_account() is None


async def test_concurrency_and_cancel_leave_no_requests_running() -> None:
    active = 0
    peak = 0
    busy = asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": f"id{i}"} for i in range(20)]})
        active += 1
        peak = max(peak, active)
        if active == 5:
            busy.set()
        try:
            await asyncio.Event().wait()
            raise AssertionError("Unreachable")
        finally:
            active -= 1

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        provider = GmailProvider(FakeSession(), GmailClient(http, FakeSession()))
        await provider.connect()

        async def collect() -> None:
            _ = [
                p
                async for p in provider.iter_message_pages(
                    range_start_utc=NOW, range_end_utc=NOW + timedelta(days=1)
                )
            ]

        task = asyncio.create_task(collect())
        await asyncio.wait_for(busy.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert peak == 5
    assert active == 0
