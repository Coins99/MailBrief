"""Body preparation service: statuses, order, limits, concurrency and leak resistance."""

import asyncio
import logging

import pytest

from mailbrief.domain.bodies import BodySource, BodyStatus, MessageBody
from mailbrief.domain.messages import RankedMessage
from mailbrief.ports.errors import (
    AuthenticationRequiredError,
    MessageUnavailableError,
    ProviderResponseError,
)
from mailbrief.services.bodies import BodyService, prepare_body
from tests.factories import make_message

MARKER = "BODY-MARKER-5519"


def ranked(message_id: str, subject: str = "Budget") -> RankedMessage:
    return RankedMessage(
        message=make_message(provider_message_id=message_id, subject=subject), score=20
    )


class FakeBodies:
    """Provider fake: returns text, raises, or waits, per message ID."""

    def __init__(self, outcomes: dict[str, str | Exception]) -> None:
        self.outcomes = outcomes
        self.active = 0
        self.peak = 0
        self.cancelled = 0

    async def fetch_message_body(self, provider_message_id: str) -> MessageBody:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.01)
            outcome = self.outcomes[provider_message_id]
            if isinstance(outcome, Exception):
                raise outcome
            source = BodySource.PLAIN if outcome else BodySource.NONE
            return MessageBody(provider_message_id=provider_message_id, text=outcome, source=source)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.active -= 1


async def test_statuses_follow_shortlist_order() -> None:
    provider = FakeBodies(
        {
            "ready": f"{MARKER} please approve.\n\nOn Monday Alex wrote:\n> old",
            "empty": "",
            "gone": MessageUnavailableError("gone"),
            "broken": ProviderResponseError("broken"),
        }
    )
    shortlist = [ranked("ready"), ranked("empty"), ranked("gone"), ranked("broken")]
    prepared = await BodyService(provider).prepare(shortlist)
    assert [body.status for body in prepared] == [
        BodyStatus.READY,
        BodyStatus.EMPTY,
        BodyStatus.UNAVAILABLE,
        BodyStatus.FAILED,
    ]
    assert prepared[0].text == f"{MARKER} please approve."
    assert prepared[0].quoted_history_removed


async def test_limit_and_extraction_truncation_are_flagged() -> None:
    long_body = MessageBody(
        provider_message_id="m1",
        text="word " * 400,
        source=BodySource.HTML,
        extraction_truncated=False,
    )
    prepared = prepare_body(long_body, subject="Report", limit=100)
    assert len(prepared.text) <= 100 and prepared.truncated
    assert prepared.original_chars == len(("word " * 400).strip())
    cut_upstream = MessageBody(
        provider_message_id="m2", text="short", source=BodySource.PLAIN, extraction_truncated=True
    )
    assert prepare_body(cut_upstream, subject=None).truncated


async def test_forward_subject_keeps_quoted_content() -> None:
    body = MessageBody(
        provider_message_id="m1",
        text="FYI\n\n-----Original Message-----\nFrom: Pat\nSent: Monday\n\nBudget",
        source=BodySource.PLAIN,
    )
    prepared = prepare_body(body, subject="FW: Budget")
    assert "Budget" in prepared.text and not prepared.quoted_history_removed


async def test_concurrency_is_bounded() -> None:
    provider = FakeBodies({f"m{i}": "text" for i in range(12)})
    await BodyService(provider, concurrency=3).prepare([ranked(f"m{i}") for i in range(12)])
    assert provider.peak == 3


async def test_sign_in_failure_stops_the_batch_and_cancels_the_rest() -> None:
    provider = FakeBodies(
        {"auth": AuthenticationRequiredError("sign in"), **{f"m{i}": "text" for i in range(4)}}
    )
    service = BodyService(provider, concurrency=5)
    with pytest.raises(AuthenticationRequiredError):
        await service.prepare([ranked("auth"), *[ranked(f"m{i}") for i in range(4)]])
    assert provider.active == 0


async def test_empty_shortlist() -> None:
    assert await BodyService(FakeBodies({})).prepare([]) == ()


@pytest.mark.parametrize(("limit", "concurrency"), [(0, 5), (8_001, 5), (100, 0)])
def test_invalid_settings_are_rejected(limit: int, concurrency: int) -> None:
    with pytest.raises(ValueError):
        BodyService(FakeBodies({}), limit=limit, concurrency=concurrency)


async def test_body_text_never_reaches_logs_or_repr(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    provider = FakeBodies({"m1": f"{MARKER} secret body"})
    prepared = await BodyService(provider).prepare([ranked("m1")])
    assert MARKER in prepared[0].text
    assert MARKER not in caplog.text
    assert MARKER not in repr(prepared)


def test_unreadable_message_is_failed_not_empty() -> None:
    unreadable = MessageBody(provider_message_id="m1", source=BodySource.NONE, unreadable_parts=1)
    assert prepare_body(unreadable, subject=None).status is BodyStatus.FAILED
    empty = MessageBody(provider_message_id="m2", source=BodySource.NONE)
    assert prepare_body(empty, subject=None).status is BodyStatus.EMPTY
    partial = MessageBody(
        provider_message_id="m3", text="Kept", source=BodySource.PLAIN, unreadable_parts=1
    )
    prepared = prepare_body(partial, subject=None)
    assert (prepared.status, prepared.unreadable_parts) == (BodyStatus.READY, 1)
