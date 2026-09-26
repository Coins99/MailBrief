"""OpenAI adapter: strict request shape, response mapping, retries and error hygiene."""

import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from mailbrief.config import Settings
from mailbrief.domain.analysis import AIUsage, AnalysisProblem, AnalysisRequest
from mailbrief.domain.messages import EmailContact
from mailbrief.ports.errors import (
    AIAuthenticationError,
    ProviderError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderRequestRejectedError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from mailbrief.providers.openai.credentials import ENTRY, SERVICE, OpenAIKeyStore
from mailbrief.providers.openai.factory import openai_provider
from mailbrief.providers.openai.provider import (
    AUTH_MESSAGE,
    INSTRUCTIONS,
    PROMPT_VERSION,
    UNREADABLE_MESSAGE,
    OpenAIProvider,
)
from tests.unit.providers.openai.openai_fixtures import (
    RESPONSES_URL,
    TEST_KEY,
    MemoryVault,
    answer_every_message,
    error_body,
    output_text,
    response_body,
    results_body,
    sent_messages,
    wire_result,
)

BODY = "Please approve the quarterly budget by Friday 5 PM."
MARKER = "OPENAI-ERROR-MARKER-5d2e"
STEP_5_3_KEYS = {
    "message_key",
    "subject",
    "sender",
    "received_local",
    "time_zone",
    "body_truncated",
    "body",
}


class RecordedSleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


@pytest.fixture
def sleeps() -> RecordedSleeps:
    return RecordedSleeps()


def store() -> OpenAIKeyStore:
    return OpenAIKeyStore(MemoryVault({(SERVICE, ENTRY): TEST_KEY}))


@pytest.fixture
async def provider(sleeps: RecordedSleeps) -> AsyncIterator[OpenAIProvider]:
    settings = Settings(openai_model="test-model", ai_max_output_tokens=4_000)
    async with openai_provider(settings, key_store=store(), sleep=sleeps) as built:
        yield built


def make_request(key: str = "0000abcd", body: str = BODY) -> AnalysisRequest:
    return AnalysisRequest(
        message_key=key,
        subject="Budget",
        sender=EmailContact(name="Alex", address="alex@example.com"),
        received_at_utc=datetime(2026, 9, 4, 13, 30, tzinfo=UTC),
        timezone_name="America/Toronto",
        body_text=body,
        body_truncated=True,
    )


def good_answer(key: str = "0000abcd") -> httpx.Response:
    return httpx.Response(200, json=results_body([wire_result(key, BODY[:40])]))


def object_schemas(schema: dict[str, Any]) -> list[dict[str, Any]]:
    nested = [value for value in schema.get("$defs", {}).values() if value.get("type") == "object"]
    return [schema, *nested]


async def test_the_request_is_strict_minimized_and_unstored(
    respx_mock: respx.MockRouter, provider: OpenAIProvider
) -> None:
    route = respx_mock.post(RESPONSES_URL).mock(side_effect=answer_every_message)

    await provider.analyze([make_request("0000abcd"), make_request("0000abce", "Second body.")])

    sent = route.calls.last.request
    body = json.loads(sent.content)
    assert sent.url == RESPONSES_URL
    assert body["model"] == "test-model"
    assert body["store"] is False
    assert body["instructions"] == INSTRUCTIONS
    assert body["max_output_tokens"] == 4_000
    text_format = body["text"]["format"]
    assert (text_format["type"], text_format["strict"]) == ("json_schema", True)
    levels = object_schemas(text_format["schema"])
    assert len(levels) == 2
    for level in levels:
        assert level["additionalProperties"] is False
        assert sorted(level["required"]) == sorted(level["properties"])
    messages = sent_messages(sent)
    assert [set(message) for message in messages] == [STEP_5_3_KEYS, STEP_5_3_KEYS]
    assert messages[0]["sender"] == {"name": "Alex", "address": "alex@example.com"}
    assert messages[0]["received_local"] == "2026-09-04 (Friday) 09:30"
    assert (messages[0]["time_zone"], messages[0]["body_truncated"]) == ("America/Toronto", True)
    assert re.fullmatch(r"[0-9a-f-]{36}", sent.headers["X-Client-Request-Id"])


async def test_a_valid_answer_becomes_candidates_with_usage(
    respx_mock: respx.MockRouter, provider: OpenAIProvider
) -> None:
    respx_mock.post(RESPONSES_URL).mock(return_value=good_answer())

    response = await provider.analyze([make_request()])

    assert response.problem is None
    (candidate,) = response.candidates
    assert (candidate.message_key, candidate.evidence) == ("0000abcd", BODY[:40])
    assert response.usage == AIUsage(input_tokens=1_200, output_tokens=300)
    assert (provider.provider_name, provider.model_name) == ("openai", "test-model")
    assert provider.prompt_version == PROMPT_VERSION


@pytest.mark.parametrize(
    ("payload", "problem"),
    [
        (
            response_body([{"type": "refusal", "refusal": "I can't help with that."}]),
            AnalysisProblem.REFUSED,
        ),
        (
            response_body([output_text('{"results": [{"message_key": "00')], status="incomplete"),
            AnalysisProblem.INCOMPLETE,
        ),
        (response_body([output_text("not json at all")]), AnalysisProblem.INVALID_OUTPUT),
        (
            response_body([output_text('{"results": [{"message_key": "0000abcd"}]}')]),
            AnalysisProblem.INVALID_OUTPUT,
        ),
        (
            response_body([output_text('{"results": [], "extra": 1}')]),
            AnalysisProblem.INVALID_OUTPUT,
        ),
        (response_body([]), AnalysisProblem.INVALID_OUTPUT),
    ],
    ids=["refusal", "incomplete", "not-json", "missing-fields", "extra-key", "no-output"],
)
async def test_problem_responses_keep_their_usage(
    respx_mock: respx.MockRouter,
    provider: OpenAIProvider,
    payload: dict[str, Any],
    problem: AnalysisProblem,
) -> None:
    respx_mock.post(RESPONSES_URL).respond(json=payload)

    response = await provider.analyze([make_request()])

    assert response.problem is problem
    assert response.candidates == ()
    assert response.usage == AIUsage(input_tokens=1_200, output_tokens=300)


@pytest.mark.parametrize(
    "output",
    [
        [{"type": "message", "role": "assistant", "content": None}],
        [{"type": "message", "role": "assistant", "content": ["just text"]}],
    ],
    ids=["content-not-a-list", "content-item-not-an-object"],
)
async def test_a_malformed_output_list_is_invalid_output(
    respx_mock: respx.MockRouter, provider: OpenAIProvider, output: list[dict[str, Any]]
) -> None:
    payload = {**response_body([]), "output": output}
    respx_mock.post(RESPONSES_URL).respond(json=payload)

    response = await provider.analyze([make_request()])

    assert response.problem is AnalysisProblem.INVALID_OUTPUT
    assert response.usage == AIUsage(input_tokens=1_200, output_tokens=300)


async def test_a_response_without_usage_reports_none(
    respx_mock: respx.MockRouter, provider: OpenAIProvider
) -> None:
    respx_mock.post(RESPONSES_URL).respond(
        json=results_body([wire_result("0000abcd", BODY[:40])], with_usage=False)
    )

    response = await provider.analyze([make_request()])

    assert response.usage is None
    assert len(response.candidates) == 1


async def test_a_short_rate_limit_is_waited_out_once(
    respx_mock: respx.MockRouter, provider: OpenAIProvider, sleeps: RecordedSleeps
) -> None:
    limited = httpx.Response(
        429, headers={"Retry-After": "2"}, json=error_body("rate_limit_exceeded")
    )
    route = respx_mock.post(RESPONSES_URL).mock(side_effect=[limited, good_answer()])

    response = await provider.analyze([make_request()])

    assert route.call_count == 2
    assert sleeps.delays == [2.0]
    assert len(response.candidates) == 1


async def test_a_long_rate_limit_fails_at_once(
    respx_mock: respx.MockRouter, provider: OpenAIProvider, sleeps: RecordedSleeps
) -> None:
    route = respx_mock.post(RESPONSES_URL).respond(
        429, headers={"Retry-After": "120"}, json=error_body("rate_limit_exceeded")
    )

    with pytest.raises(ProviderRateLimitError) as caught:
        await provider.analyze([make_request()])

    assert route.call_count == 1
    assert sleeps.delays == []
    assert caught.value.retry_after_seconds == 120.0
    assert str(caught.value) == "OpenAI rate limit reached; retry later."


@pytest.mark.parametrize(
    ("status", "code", "error_type", "message"),
    [
        (429, "insufficient_quota", ProviderPermissionError, "OpenAI denied access"),
        (401, "invalid_api_key", AIAuthenticationError, AUTH_MESSAGE),
        (403, "unsupported_country_region_territory", ProviderPermissionError, "denied access"),
        (400, "invalid_prompt", ProviderRequestRejectedError, "rejected the request (HTTP 400)"),
        (413, "request_too_large", ProviderRequestRejectedError, "rejected the request (HTTP 413)"),
        (422, "unprocessable", ProviderRequestRejectedError, "rejected the request (HTTP 422)"),
        (404, "model_not_found", ProviderResponseError, "rejected the request (HTTP 404)"),
    ],
)
async def test_terminal_errors_carry_static_messages_and_safe_codes(
    respx_mock: respx.MockRouter,
    provider: OpenAIProvider,
    status: int,
    code: str,
    error_type: type[ProviderError],
    message: str,
) -> None:
    route = respx_mock.post(RESPONSES_URL).respond(status, json=error_body(code))

    with pytest.raises(error_type, match=re.escape(message)) as caught:
        await provider.analyze([make_request()])

    assert type(caught.value) is error_type
    assert route.call_count == 1
    assert caught.value.provider_error_code == code
    assert caught.value.client_request_id is not None


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(200, text="<html>maintenance</html>", headers={"content-type": "text/html"}),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, json={"error": {"message": "Something went wrong."}}),
    ],
    ids=["html", "json-array", "no-output-list"],
)
async def test_an_unreadable_reply_is_unavailable_without_retry(
    respx_mock: respx.MockRouter,
    provider: OpenAIProvider,
    sleeps: RecordedSleeps,
    reply: httpx.Response,
) -> None:
    route = respx_mock.post(RESPONSES_URL).mock(return_value=reply)

    with pytest.raises(ProviderUnavailableError) as caught:
        await provider.analyze([make_request()])

    assert str(caught.value) == UNREADABLE_MESSAGE == "OpenAI returned an unreadable response."
    assert caught.value.client_request_id is not None
    assert route.call_count == 1
    assert sleeps.delays == []


async def test_an_unsafe_error_code_is_dropped(
    respx_mock: respx.MockRouter, provider: OpenAIProvider
) -> None:
    respx_mock.post(RESPONSES_URL).respond(401, json=error_body("bad code; with spaces"))

    with pytest.raises(AIAuthenticationError) as caught:
        await provider.analyze([make_request()])

    assert caught.value.provider_error_code is None


@pytest.mark.parametrize("status", [500, 503, 408])
async def test_server_errors_are_retried_three_times_then_unavailable(
    respx_mock: respx.MockRouter, provider: OpenAIProvider, sleeps: RecordedSleeps, status: int
) -> None:
    route = respx_mock.post(RESPONSES_URL).respond(status, json=error_body("server_error"))

    with pytest.raises(ProviderUnavailableError) as caught:
        await provider.analyze([make_request()])

    assert str(caught.value) == f"OpenAI is unavailable (HTTP {status}); retry later."
    assert route.call_count == 4
    assert sleeps.delays == [1.0, 2.0, 4.0]


@pytest.mark.parametrize(
    ("failure", "error_type", "message"),
    [
        (httpx.ConnectError("unreachable"), ProviderError, "Could not reach OpenAI."),
        (httpx.ReadTimeout("slow"), ProviderTimeoutError, "OpenAI did not respond in time."),
    ],
    ids=["connection", "timeout"],
)
async def test_transport_failures_are_retried_then_raised(
    respx_mock: respx.MockRouter,
    provider: OpenAIProvider,
    sleeps: RecordedSleeps,
    failure: Exception,
    error_type: type[ProviderError],
    message: str,
) -> None:
    route = respx_mock.post(RESPONSES_URL).mock(side_effect=failure)

    with pytest.raises(ProviderError) as caught:
        await provider.analyze([make_request()])

    assert type(caught.value) is error_type
    assert str(caught.value) == message
    assert route.call_count == 4
    assert sleeps.delays == [1.0, 2.0, 4.0]


async def test_openai_error_text_never_reaches_exceptions_or_logs(
    respx_mock: respx.MockRouter, provider: OpenAIProvider, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    respx_mock.post(RESPONSES_URL).respond(
        401, json=error_body("invalid_api_key", f"Incorrect API key provided: {MARKER}")
    )

    with pytest.raises(AIAuthenticationError) as caught:
        await provider.analyze([make_request()])

    assert str(caught.value) == AUTH_MESSAGE
    assert MARKER not in repr(caught.value)
    assert MARKER not in caplog.text


async def test_the_base_url_ignores_the_environment(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch, sleeps: RecordedSleeps
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    route = respx_mock.post(RESPONSES_URL).mock(return_value=good_answer())

    async with openai_provider(
        Settings(openai_model="test-model"), key_store=store(), sleep=sleeps
    ) as built:
        await built.analyze([make_request()])

    assert route.call_count == 1


@pytest.mark.parametrize("count", [0, 11])
async def test_a_call_needs_one_to_ten_requests(provider: OpenAIProvider, count: int) -> None:
    requests = [make_request(f"{index:08x}") for index in range(count)]

    with pytest.raises(ValueError, match="1 to 10"):
        await provider.analyze(requests)


async def test_a_call_needs_unique_keys(provider: OpenAIProvider) -> None:
    with pytest.raises(ValueError, match="unique"):
        await provider.analyze([make_request("0000abcd"), make_request("0000abcd")])
