"""Groq adapter: strict request shape, response mapping, retries and error hygiene."""

import asyncio
import hashlib
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
    ProviderUsageLimitError,
)
from mailbrief.providers.groq.credentials import ENTRY, SERVICE, GroqKeyStore
from mailbrief.providers.groq.factory import groq_provider
from mailbrief.providers.groq.provider import (
    AUTH_MESSAGE,
    INSTRUCTIONS,
    PROMPT_VERSION,
    UNREADABLE_MESSAGE,
    AnalysisWireBatch,
    GroqProvider,
)
from tests.unit.providers.groq.groq_fixtures import (
    CHAT_URL,
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
MARKER = "GROQ-ERROR-MARKER-5d2e"
REQUEST_KEYS = {"model", "messages", "response_format", "max_completion_tokens"}
PINNED_PROMPT = (
    "groq-2026-09-26.1",
    "0454a48e855e876090802d43f7e8f919cf7f029916323748cff486f79d5a2b58",
)
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


def store() -> GroqKeyStore:
    return GroqKeyStore(MemoryVault({(SERVICE, ENTRY): TEST_KEY}))


@pytest.fixture
async def provider(sleeps: RecordedSleeps) -> AsyncIterator[GroqProvider]:
    settings = Settings(groq_model="test-model", ai_max_output_tokens=4_000)
    async with groq_provider(settings, key_store=store(), sleep=sleeps) as built:
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


async def test_the_request_is_strict_and_minimized(
    respx_mock: respx.MockRouter, provider: GroqProvider
) -> None:
    route = respx_mock.post(CHAT_URL).mock(side_effect=answer_every_message)

    await provider.analyze([make_request("0000abcd"), make_request("0000abce", "Second body.")])

    sent = route.calls.last.request
    body = json.loads(sent.content)
    assert sent.url == CHAT_URL
    assert set(body) == REQUEST_KEYS
    assert body["model"] == "test-model"
    assert "store" not in body
    assert sent.headers["Authorization"] == f"Bearer {TEST_KEY}"
    assert body["messages"][0]["content"] == INSTRUCTIONS
    assert body["max_completion_tokens"] == 4_000
    assert body["response_format"]["type"] == "json_schema"
    text_format = body["response_format"]["json_schema"]
    assert text_format["strict"] is True
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


def test_the_prompt_version_changes_with_the_prompt_or_schema() -> None:
    schema = json.dumps(
        AnalysisWireBatch.model_json_schema(), sort_keys=True, separators=(",", ":")
    )
    fingerprint = hashlib.sha256(f"{INSTRUCTIONS}\n{schema}".encode()).hexdigest()

    assert (PROMPT_VERSION, fingerprint) == PINNED_PROMPT, (
        "prompt or schema changed: bump PROMPT_VERSION and this hash"
    )


async def test_a_valid_answer_becomes_candidates_with_usage(
    respx_mock: respx.MockRouter, provider: GroqProvider
) -> None:
    respx_mock.post(CHAT_URL).mock(return_value=good_answer())

    response = await provider.analyze([make_request()])

    assert response.problem is None
    (candidate,) = response.candidates
    assert (candidate.message_key, candidate.evidence) == ("0000abcd", BODY[:40])
    assert response.usage == AIUsage(input_tokens=1_200, output_tokens=300)
    assert (provider.provider_name, provider.model_name) == ("groq", "test-model")
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
    provider: GroqProvider,
    payload: dict[str, Any],
    problem: AnalysisProblem,
) -> None:
    respx_mock.post(CHAT_URL).respond(json=payload)

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
    respx_mock: respx.MockRouter, provider: GroqProvider, output: list[dict[str, Any]]
) -> None:
    payload = {**response_body([]), "choices": output}
    respx_mock.post(CHAT_URL).respond(json=payload)

    response = await provider.analyze([make_request()])

    assert response.problem is AnalysisProblem.INVALID_OUTPUT
    assert response.usage == AIUsage(input_tokens=1_200, output_tokens=300)


async def test_a_response_without_usage_reports_none(
    respx_mock: respx.MockRouter, provider: GroqProvider
) -> None:
    respx_mock.post(CHAT_URL).respond(
        json=results_body([wire_result("0000abcd", BODY[:40])], with_usage=False)
    )

    response = await provider.analyze([make_request()])

    assert response.usage is None
    assert len(response.candidates) == 1


@pytest.mark.parametrize("status", ["failed", "cancelled", "queued", "in_progress", None])
async def test_only_completed_responses_can_supply_candidates(
    respx_mock: respx.MockRouter, provider: GroqProvider, status: str | None
) -> None:
    payload = results_body([wire_result("0000abcd", BODY[:40])])
    payload["choices"][0]["finish_reason"] = status
    respx_mock.post(CHAT_URL).respond(json=payload)

    response = await provider.analyze([make_request()])

    assert response.problem is AnalysisProblem.INVALID_OUTPUT
    assert response.candidates == ()


@pytest.mark.parametrize("output", [None, [None], [{"type": "message", "content": None}]])
async def test_malformed_envelopes_are_invalid_output(
    respx_mock: respx.MockRouter, provider: GroqProvider, output: object
) -> None:
    payload = results_body([wire_result("0000abcd", BODY[:40])])
    payload["choices"] = output
    respx_mock.post(CHAT_URL).respond(json=payload)

    response = await provider.analyze([make_request()])

    assert response.problem is AnalysisProblem.INVALID_OUTPUT
    assert response.candidates == ()


async def test_a_short_rate_limit_is_waited_out_once(
    respx_mock: respx.MockRouter, provider: GroqProvider, sleeps: RecordedSleeps
) -> None:
    limited = httpx.Response(
        429, headers={"Retry-After": "2"}, json=error_body("rate_limit_exceeded")
    )
    route = respx_mock.post(CHAT_URL).mock(side_effect=[limited, good_answer()])

    response = await provider.analyze([make_request()])

    assert route.call_count == 2
    assert sleeps.delays == [2.0]
    assert len(response.candidates) == 1


async def test_a_long_rate_limit_fails_at_once(
    respx_mock: respx.MockRouter, provider: GroqProvider, sleeps: RecordedSleeps
) -> None:
    route = respx_mock.post(CHAT_URL).respond(
        429, headers={"Retry-After": "120"}, json=error_body("rate_limit_exceeded")
    )

    with pytest.raises(ProviderRateLimitError) as caught:
        await provider.analyze([make_request()])

    assert route.call_count == 1
    assert sleeps.delays == []
    assert caught.value.retry_after_seconds == 120.0
    assert str(caught.value) == "Groq rate limit reached; retry later."


@pytest.mark.parametrize(
    ("status", "code", "error_type", "message"),
    [
        (429, "insufficient_quota", ProviderPermissionError, "Groq denied access"),
        (400, "blocked_api_access", ProviderPermissionError, "Groq denied access"),
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
    provider: GroqProvider,
    status: int,
    code: str,
    error_type: type[ProviderError],
    message: str,
) -> None:
    route = respx_mock.post(CHAT_URL).respond(status, json=error_body(code))

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
    provider: GroqProvider,
    sleeps: RecordedSleeps,
    reply: httpx.Response,
) -> None:
    route = respx_mock.post(CHAT_URL).mock(return_value=reply)

    with pytest.raises(ProviderUnavailableError) as caught:
        await provider.analyze([make_request()])

    assert str(caught.value) == UNREADABLE_MESSAGE == "Groq returned an unreadable response."
    assert caught.value.client_request_id is not None
    assert route.call_count == 1
    assert sleeps.delays == []


async def test_an_unsafe_error_code_is_dropped(
    respx_mock: respx.MockRouter, provider: GroqProvider
) -> None:
    respx_mock.post(CHAT_URL).respond(401, json=error_body("bad code; with spaces"))

    with pytest.raises(AIAuthenticationError) as caught:
        await provider.analyze([make_request()])

    assert caught.value.provider_error_code is None


@pytest.mark.parametrize("status", [500, 503, 408])
async def test_server_errors_are_retried_three_times_then_unavailable(
    respx_mock: respx.MockRouter, provider: GroqProvider, sleeps: RecordedSleeps, status: int
) -> None:
    route = respx_mock.post(CHAT_URL).respond(status, json=error_body("server_error"))

    with pytest.raises(ProviderUnavailableError) as caught:
        await provider.analyze([make_request()])

    assert str(caught.value) == f"Groq is unavailable (HTTP {status}); retry later."
    assert route.call_count == 4
    assert sleeps.delays == [1.0, 2.0, 4.0]


@pytest.mark.parametrize(
    ("failure", "error_type", "message"),
    [
        (httpx.ConnectError("unreachable"), ProviderError, "Could not reach Groq."),
        (httpx.ReadTimeout("slow"), ProviderTimeoutError, "Groq did not respond in time."),
    ],
    ids=["connection", "timeout"],
)
async def test_transport_failures_are_retried_then_raised(
    respx_mock: respx.MockRouter,
    provider: GroqProvider,
    sleeps: RecordedSleeps,
    failure: Exception,
    error_type: type[ProviderError],
    message: str,
) -> None:
    route = respx_mock.post(CHAT_URL).mock(side_effect=failure)

    with pytest.raises(ProviderError) as caught:
        await provider.analyze([make_request()])

    assert type(caught.value) is error_type
    assert str(caught.value) == message
    assert route.call_count == 4
    assert sleeps.delays == [1.0, 2.0, 4.0]


async def test_groq_error_text_never_reaches_exceptions_or_logs(
    respx_mock: respx.MockRouter, provider: GroqProvider, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    respx_mock.post(CHAT_URL).respond(
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
    monkeypatch.setenv("GROQ_BASE_URL", "https://example.invalid/v1")
    route = respx_mock.post(CHAT_URL).mock(return_value=good_answer())

    async with groq_provider(
        Settings(groq_model="test-model"), key_store=store(), sleep=sleeps
    ) as built:
        await built.analyze([make_request()])

    assert route.call_count == 1


@pytest.mark.parametrize("count", [0, 11])
async def test_a_call_needs_one_to_ten_requests(provider: GroqProvider, count: int) -> None:
    requests = [make_request(f"{index:08x}") for index in range(count)]

    with pytest.raises(ValueError, match="1 to 10"):
        await provider.analyze(requests)


async def test_a_call_needs_unique_keys(provider: GroqProvider) -> None:
    with pytest.raises(ValueError, match="unique"):
        await provider.analyze([make_request("0000abcd"), make_request("0000abcd")])


async def test_request_budget_spans_calls_and_resets_only_for_a_new_connection(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.post(CHAT_URL).mock(return_value=good_answer())
    settings = Settings(groq_model="test-model", ai_max_requests_per_run=1)
    async with groq_provider(settings, key_store=store()) as built:
        await built.analyze([make_request()])
        with pytest.raises(ProviderUsageLimitError, match="request limit"):
            await built.analyze([make_request()])
    assert route.call_count == 1

    async with groq_provider(settings, key_store=store()) as built:
        await built.analyze([make_request()])
    assert route.call_count == 2


async def test_request_budget_counts_retries_and_stops_before_an_extra_sleep(
    respx_mock: respx.MockRouter, sleeps: RecordedSleeps
) -> None:
    route = respx_mock.post(CHAT_URL).respond(500, json=error_body("server_error"))
    settings = Settings(groq_model="test-model", ai_max_requests_per_run=2)
    async with groq_provider(settings, key_store=store(), sleep=sleeps) as built:
        with pytest.raises(ProviderUsageLimitError):
            await built.analyze([make_request()])
    assert route.call_count == 2
    assert sleeps.delays == [1.0]


@pytest.mark.parametrize("resource", ["tokens", "requests"])
async def test_exhausted_quota_paces_the_next_call(
    respx_mock: respx.MockRouter, provider: GroqProvider, sleeps: RecordedSleeps, resource: str
) -> None:
    first = good_answer()
    first.headers.update(
        {
            f"x-ratelimit-remaining-{resource}": "0",
            f"x-ratelimit-reset-{resource}": "2s",
        }
    )
    route = respx_mock.post(CHAT_URL).mock(side_effect=[first, good_answer()])
    await provider.analyze([make_request()])
    assert sleeps.delays == []
    await provider.analyze([make_request()])
    assert sleeps.delays == [2.0]
    assert route.call_count == 2


@pytest.mark.parametrize("reset", ["1h", "nonsense", ""])
async def test_long_or_unknown_exhausted_quota_stops_before_another_request(
    respx_mock: respx.MockRouter, provider: GroqProvider, sleeps: RecordedSleeps, reset: str
) -> None:
    first = good_answer()
    first.headers.update({"x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": reset})
    route = respx_mock.post(CHAT_URL).mock(return_value=first)
    await provider.analyze([make_request()])
    with pytest.raises(ProviderRateLimitError):
        await provider.analyze([make_request()])
    assert route.call_count == 1
    assert sleeps.delays == []


async def test_cancellation_is_not_retried(
    respx_mock: respx.MockRouter, provider: GroqProvider, sleeps: RecordedSleeps
) -> None:
    attempts = 0

    async def cancel(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise asyncio.CancelledError

    respx_mock.post(CHAT_URL).mock(side_effect=cancel)
    with pytest.raises(asyncio.CancelledError):
        await provider.analyze([make_request()])
    assert attempts == 1
    assert sleeps.delays == []


@pytest.mark.parametrize(
    "message",
    [
        None,
        {},
        {"role": "user", "content": "{}"},
        {"role": "assistant", "content": []},
        {"role": "assistant", "content": "{}", "tool_calls": [{"id": "unexpected"}]},
    ],
)
async def test_invalid_chat_messages_never_supply_candidates(
    respx_mock: respx.MockRouter, provider: GroqProvider, message: object
) -> None:
    payload = results_body([wire_result("0000abcd", BODY[:40])])
    payload["choices"][0]["message"] = message
    respx_mock.post(CHAT_URL).respond(json=payload)
    response = await provider.analyze([make_request()])
    assert response.problem is AnalysisProblem.INVALID_OUTPUT
    assert response.candidates == ()


async def test_redirect_cannot_forward_key_or_mail(
    respx_mock: respx.MockRouter, provider: GroqProvider
) -> None:
    route = respx_mock.post(CHAT_URL).respond(307, headers={"Location": "https://evil.invalid/"})
    with pytest.raises(ProviderResponseError):
        await provider.analyze([make_request()])
    assert route.call_count == 1
    assert len(respx_mock.calls) == 1
