"""Gmail body extraction: MIME selection, decoding, attachments and limits."""

import httpx
import pytest
import respx

from mailbrief.domain.bodies import MAX_EXTRACTED_CHARS, BodySource
from mailbrief.ports.errors import MessageUnavailableError, ProviderResponseError
from mailbrief.providers.gmail.body import MAX_SEPARATE_PART_BYTES, extract_body
from mailbrief.providers.gmail.client import MESSAGES_URL, GmailClient
from mailbrief.providers.gmail.provider import GmailProvider
from tests.unit.providers.gmail.body_fixtures import encode, message, part
from tests.unit.providers.gmail.metadata_fixtures import FakeSession, no_sleep

LONG_PLAIN = "Plain text body. " * 40


class PartStore:
    """Fake fetcher for separately stored parts that records every request."""

    def __init__(self, parts: dict[str, bytes] | None = None) -> None:
        self.parts = parts or {}
        self.requested: list[str] = []

    async def __call__(self, attachment: str) -> dict[str, object] | None:
        self.requested.append(attachment)
        content = self.parts.get(attachment)
        return None if content is None else {"size": len(content), "data": encode(content)}


def alternative(plain: str, html: str) -> dict[str, object]:
    return part("multipart/alternative", parts=[part("text/plain", plain), part("text/html", html)])


async def test_plain_alternative_is_preferred() -> None:
    body = await extract_body(
        message(alternative(LONG_PLAIN, "<p>HTML version</p>")), "m1", PartStore()
    )
    assert body.source is BodySource.PLAIN
    assert body.text == LONG_PLAIN.strip()


async def test_stub_plain_part_falls_back_to_html() -> None:
    html = "<p>" + "Real newsletter content. " * 20 + "</p>"
    body = await extract_body(
        message(alternative("View this email in your browser.", html)), "m1", PartStore()
    )
    assert body.source is BodySource.HTML
    assert body.text.startswith("Real newsletter content.")


async def test_html_only_message() -> None:
    body = await extract_body(
        message(part("text/html", "<p>Only <b>HTML</b></p>")), "m1", PartStore()
    )
    assert (body.source, body.text) == (BodySource.HTML, "Only HTML")


async def test_attachments_are_counted_and_never_fetched() -> None:
    store = PartStore({"pdf": b"%PDF secret"})
    payload = part(
        "multipart/mixed",
        parts=[
            alternative(LONG_PLAIN, "<p>x</p>"),
            part("application/pdf", filename="contract.pdf", attachment_id="pdf", size=11),
            part("text/plain", "attached notes", filename="notes.txt"),
        ],
    )
    body = await extract_body(message(payload), "m1", store)
    assert body.attachments_skipped == 2
    assert store.requested == []
    assert "attached notes" not in body.text


async def test_separately_stored_text_part_is_fetched() -> None:
    store = PartStore({"big-html": b"<p>Stored separately</p>"})
    payload = part("text/html", attachment_id="big-html", size=24)
    body = await extract_body(message(payload), "m1", store)
    assert (body.source, body.text) == (BodySource.HTML, "Stored separately")
    assert store.requested == ["big-html"]


async def test_oversized_separate_part_is_skipped() -> None:
    store = PartStore({"huge": b"x"})
    payload = part("text/plain", attachment_id="huge", size=MAX_SEPARATE_PART_BYTES + 1)
    body = await extract_body(message(payload), "m1", store)
    assert (body.source, body.unreadable_parts, store.requested) == (BodySource.NONE, 1, [])


async def test_utf8_is_used_despite_a_legacy_charset_label() -> None:
    payload = part(
        "text/plain",
        "日本語のメール",
        headers={"Content-Type": 'text/plain; charset="iso-2022-jp"'},
    )
    body = await extract_body(message(payload), "m1", PartStore())
    assert body.text == "日本語のメール"


async def test_declared_charset_is_the_fallback_for_non_utf8_bytes() -> None:
    payload = part(
        "text/plain",
        "café".encode("latin-1"),
        headers={"Content-Type": "text/plain; charset=iso-8859-1"},
    )
    body = await extract_body(message(payload), "m1", PartStore())
    assert body.text == "café"


async def test_quoted_printable_label_is_not_decoded_twice() -> None:
    text = "Open https://example.test/?a=1&b=2 and keep =3D as typed."
    payload = part(
        "text/plain",
        text,
        headers={"Content-Transfer-Encoding": "quoted-printable", "Content-Type": "text/plain"},
    )
    body = await extract_body(message(payload), "m1", PartStore())
    assert body.text == text


async def test_malformed_part_is_counted_and_the_rest_is_kept() -> None:
    broken = part("text/plain", "ignored")
    broken["body"] = {"size": 5, "data": "@@@not-base64@@@"}
    payload = part("multipart/mixed", parts=[broken, part("text/plain", "Still readable")])
    body = await extract_body(message(payload), "m1", PartStore())
    assert (body.text, body.unreadable_parts) == ("Still readable", 1)


class FailingStore(PartStore):
    async def __call__(self, attachment: str) -> dict[str, object] | None:
        self.requested.append(attachment)
        raise ProviderResponseError("temporary failure")


@pytest.mark.parametrize(
    "store",
    [PartStore(), PartStore({"p1": b""}), FailingStore()],
    ids=["missing", "empty", "failing"],
)
async def test_unreadable_separate_parts_are_counted(store: PartStore) -> None:
    payload = part(
        "multipart/mixed",
        parts=[part("text/html", attachment_id="p1", size=10), part("text/plain", "Kept")],
    )
    body = await extract_body(message(payload), "m1", store)
    assert (body.text, body.unreadable_parts, store.requested) == ("Kept", 1, ["p1"])


@pytest.mark.parametrize("charset", ["x-unknown-set", "idna", "undefined"])
async def test_unknown_charset_falls_back_to_replacement(charset: str) -> None:
    payload = part(
        "text/plain", b"caf\xe9", headers={"Content-Type": f"text/plain; charset={charset}"}
    )
    body = await extract_body(message(payload), "m1", PartStore())
    assert body.text == "caf\ufffd"


@pytest.mark.parametrize("children", ["not-a-list", ["not-a-part"]])
async def test_malformed_children_are_rejected(children: object) -> None:
    payload = part("multipart/mixed", parts=[])
    payload["parts"] = children
    with pytest.raises(ProviderResponseError):
        await extract_body(message(payload), "m1", PartStore())


async def test_message_without_text_has_no_body() -> None:
    payload = part("multipart/mixed", parts=[part("image/png", filename="scan.png", size=10)])
    body = await extract_body(message(payload), "m1", PartStore())
    assert (body.source, body.text, body.attachments_skipped) == (BodySource.NONE, "", 1)


async def test_extracted_text_is_capped() -> None:
    payload = part("text/plain", "x" * (MAX_EXTRACTED_CHARS + 50))
    body = await extract_body(message(payload), "m1", PartStore())
    assert len(body.text) == MAX_EXTRACTED_CHARS
    assert body.extraction_truncated


@pytest.mark.parametrize(
    "raw",
    [
        {"id": "other", "payload": part("text/plain", "x")},
        {"id": "m1", "payload": "not-a-part"},
    ],
)
async def test_malformed_messages_are_rejected(raw: dict[str, object]) -> None:
    with pytest.raises(ProviderResponseError):
        await extract_body(raw, "m1", PartStore())


async def test_overly_deep_structure_is_rejected() -> None:
    payload = part("text/plain", "leaf")
    for _ in range(25):
        payload = part("multipart/mixed", parts=[payload])
    with pytest.raises(ProviderResponseError):
        await extract_body(message(payload), "m1", PartStore())


async def test_provider_requests_full_format_and_separate_parts(
    respx_mock: respx.MockRouter,
) -> None:
    payload = part(
        "multipart/mixed",
        parts=[
            part("text/html", attachment_id="p1", size=20),
            part("application/zip", filename="a.zip", attachment_id="z1", size=9),
        ],
    )
    get_message = respx_mock.get(MESSAGES_URL + "/m1").respond(json=message(payload))
    get_part = respx_mock.get(MESSAGES_URL + "/m1/attachments/p1").respond(
        json={"size": 20, "data": encode("<p>From Gmail</p>")}
    )
    zip_route = respx_mock.get(MESSAGES_URL + "/m1/attachments/z1").respond(json={})
    async with httpx.AsyncClient() as http:
        provider = GmailProvider(FakeSession(), GmailClient(http, FakeSession(), sleep=no_sleep))
        body = await provider.fetch_message_body("m1")
    assert (body.text, body.attachments_skipped) == ("From Gmail", 1)
    assert get_message.calls.last.request.url.params["format"] == "full"
    assert get_part.called and not zip_route.called


async def test_deleted_message_is_unavailable(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(MESSAGES_URL + "/gone").respond(404)
    async with httpx.AsyncClient() as http:
        provider = GmailProvider(FakeSession(), GmailClient(http, FakeSession(), sleep=no_sleep))
        with pytest.raises(MessageUnavailableError):
            await provider.fetch_message_body("gone")
