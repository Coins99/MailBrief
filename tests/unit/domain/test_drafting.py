"""Drafting models: limits, what a request holds, part sizes and outcome rules."""

from datetime import UTC, date, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from mailbrief.domain.analysis import ActionOwnership
from mailbrief.domain.drafting import (
    ActionContext,
    CurrentText,
    DraftCandidate,
    DraftContextPart,
    DraftGenerationInfo,
    DraftingOptions,
    DraftingOutcome,
    DraftingPreview,
    DraftingProblem,
    DraftingRequest,
    DraftingResponse,
    DraftingStatus,
    GeneratedDraft,
    PreviewLine,
    SourceContext,
    part_sizes,
)
from mailbrief.domain.drafts import (
    Draft,
    DraftGenerationSummary,
    DraftKind,
    DraftLength,
    DraftTone,
    DraftVersionInfo,
    DraftVersionOrigin,
)

AT = datetime(2026, 9, 28, 13, tzinfo=UTC)
SOURCE = SourceContext(
    subject="Budget",
    sender_name="Alex",
    received_local="2026-09-28 (Monday) 09:30",
    body="  Body with spaces  ",
    body_truncated=False,
)
ACTION = ActionContext(
    title="Send it",
    ownership=ActionOwnership.MINE,
    target_date=date(2026, 10, 1),
    deadline_text="Friday",
    steps=("One", "Two"),
    notes="Notes",
)


def request(**parts: Any) -> DraftingRequest:
    return DraftingRequest(
        kind=DraftKind.REPLY,
        tone=DraftTone.NEUTRAL,
        length=DraftLength.MEDIUM,
        instructions="Be brief",
        today=date(2026, 9, 28),
        **parts,
    )


def test_options_default_and_limit_instructions() -> None:
    options = DraftingOptions()
    assert (options.parts, options.tone, options.length) == (
        frozenset(),
        DraftTone.NEUTRAL,
        DraftLength.MEDIUM,
    )
    assert len(DraftingOptions(instructions="x" * 1_000).instructions) == 1_000
    with pytest.raises(ValidationError) as caught:
        DraftingOptions(instructions="secret" + "x" * 1_000)
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    ("model", "field", "limit"),
    [(ActionContext, "notes", 2_000), (CurrentText, "body", 8_000)],
)
def test_sent_text_is_capped(model: Any, field: str, limit: int) -> None:
    base: dict[str, Any] = (
        {"title": "T", "ownership": ActionOwnership.MINE} if model is ActionContext else {}
    )
    assert len(getattr(model(**base, **{field: "x" * limit}), field)) == limit
    with pytest.raises(ValidationError):
        model(**base, **{field: "x" * (limit + 1)})


def test_the_steps_sent_are_capped_in_all() -> None:
    steps = ("x" * 500,) * 4
    assert ActionContext(title="T", ownership=ActionOwnership.MINE, steps=steps).steps == steps
    with pytest.raises(ValidationError):
        ActionContext(title="T", ownership=ActionOwnership.MINE, steps=(*steps, "x"))


def test_a_request_keeps_text_as_given_and_names_its_parts() -> None:
    made = request(source=SOURCE, current=CurrentText(body=" mine "))
    assert made.source is not None and made.source.body == "  Body with spaces  "
    assert made.parts == {DraftContextPart.SOURCE_EMAIL, DraftContextPart.CURRENT_TEXT}
    assert request().parts == frozenset()
    assert "Body with spaces" not in repr(made)


def test_a_source_has_a_name_but_no_address_field() -> None:
    with pytest.raises(ValidationError):
        SourceContext.model_validate({**SOURCE.model_dump(), "sender_address": "alex@example.com"})


def test_part_sizes_count_exactly_what_each_part_sends() -> None:
    sizes = part_sizes(request(source=SOURCE, action=ACTION, current=CurrentText(title="T")))

    assert sizes[None] == len("Be brief") + len("2026-09-28")
    assert sizes[DraftContextPart.SOURCE_EMAIL] == len("Budget") + len("Alex") + len(
        "2026-09-28 (Monday) 09:30"
    ) + len("  Body with spaces  ")
    assert sizes[DraftContextPart.ACTION] == len("Send it") + len("2026-10-01") + len(
        "Friday"
    ) + len("OneTwo") + len("Notes")
    assert sizes[DraftContextPart.CURRENT_TEXT] == 1
    only = part_sizes(request())
    assert set(only) == {None}


def test_a_response_has_a_candidate_or_a_problem() -> None:
    candidate = DraftCandidate(body="Hi")
    assert DraftingResponse(candidate=candidate).problem is None
    assert DraftingResponse(problem=DraftingProblem.REFUSED).candidate is None
    with pytest.raises(ValidationError):
        DraftingResponse()
    with pytest.raises(ValidationError):
        DraftingResponse(candidate=candidate, problem=DraftingProblem.INCOMPLETE)


def test_generated_drafts_are_bounded() -> None:
    GeneratedDraft(subject="S" * 200, body="B" * 8_000, missing_context=("x" * 200,) * 5)
    for bad in (
        {"body": ""},
        {"body": "B" * 8_001},
        {"body": "B", "subject": "S" * 201},
        {"body": "B", "subject": ""},
        {"body": "B", "missing_context": ("x",) * 6},
        {"body": "B", "missing_context": ("x" * 201,)},
        {"body": "B", "missing_context": ("",)},
    ):
        with pytest.raises(ValidationError):
            GeneratedDraft(**bad)


def test_preview_totals_its_lines() -> None:
    preview = DraftingPreview(
        provider="groq",
        model="m",
        privacy_notice="n",
        first_use=True,
        lines=(PreviewLine(label="a", characters=3), PreviewLine(label="b", characters=4)),
    )
    assert preview.total_characters == 7


def test_generation_info_summarizes_itself() -> None:
    info = DraftGenerationInfo(
        provider="groq",
        model="openai/gpt-oss-120b",
        prompt_version="p",
        tone=DraftTone.WARM,
        length=DraftLength.SHORT,
        parts=frozenset({DraftContextPart.ACTION}),
        created_at_utc=AT,
    )
    assert info.summary() == DraftGenerationSummary(
        tone=DraftTone.WARM, length=DraftLength.SHORT, model="openai/gpt-oss-120b"
    )
    version = DraftVersionInfo(
        number=3,
        origin=DraftVersionOrigin.GENERATED,
        created_at_utc=AT,
        preview="",
        length=0,
        generation=info.summary(),
    )
    assert version.generation is not None


def draft() -> Draft:
    return Draft(
        public_id="00000000-0000-4000-8000-000000000001",
        kind=DraftKind.NOTE,
        created_at_utc=AT,
        updated_at_utc=AT,
        revision=1,
    )


def test_outcomes_carry_what_their_status_needs() -> None:
    DraftingOutcome(status=DraftingStatus.GENERATED, draft=draft(), version_number=2)
    DraftingOutcome(status=DraftingStatus.DECLINED)
    DraftingOutcome(status=DraftingStatus.FAILED, error_code="AI_TIMEOUT")
    for bad in (
        {"status": DraftingStatus.GENERATED},
        {"status": DraftingStatus.DECLINED, "draft": draft(), "version_number": 2},
        {"status": DraftingStatus.FAILED},
        {"status": DraftingStatus.CANCELLED, "error_code": "AI_TIMEOUT"},
    ):
        with pytest.raises(ValidationError):
            DraftingOutcome(**bad)
