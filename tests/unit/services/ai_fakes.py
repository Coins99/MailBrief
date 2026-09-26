"""Scriptable AI provider fake and candidate helpers for service tests."""

from collections.abc import Callable, Sequence

from mailbrief.domain.analysis import AIUsage, AnalysisCandidate, AnalysisRequest, AnalysisResponse

Respond = Callable[[Sequence[AnalysisRequest]], AnalysisResponse]
ScriptItem = AnalysisResponse | Exception | Respond


class FakeAIProvider:
    """AIProvider fake: consumes one script item per analyze() call and records each batch."""

    def __init__(self, script: Sequence[ScriptItem] = ()) -> None:
        self.script = list(script)
        self.batches: list[tuple[AnalysisRequest, ...]] = []

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return "fake-model"

    @property
    def prompt_version(self) -> str:
        return "fake-1"

    @property
    def calls(self) -> int:
        return len(self.batches)

    async def analyze(self, requests: Sequence[AnalysisRequest]) -> AnalysisResponse:
        self.batches.append(tuple(requests))
        if not self.script:
            raise AssertionError("unexpected analyze() call")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, AnalysisResponse):
            return item
        return item(requests)


def good_candidate(request: AnalysisRequest, **overrides: object) -> AnalysisCandidate:
    """A candidate that passes validation; its evidence is a slice of the request body."""
    values: dict[str, object] = {
        "message_key": request.message_key,
        "category": "information",
        "summary": "A short summary.",
        "action_required": False,
        "action_text": None,
        "deadline_text": None,
        "deadline_date": None,
        "deadline_time": None,
        "stated_timezone": None,
        "confidence": 0.8,
        "evidence": request.body_text[:40],
    }
    values.update(overrides)
    return AnalysisCandidate.model_validate(values)


def answer_all(usage: AIUsage | None = None, **overrides: object) -> Respond:
    """A script item that answers every request in its batch with a good candidate."""

    def respond(requests: Sequence[AnalysisRequest]) -> AnalysisResponse:
        candidates = tuple(good_candidate(request, **overrides) for request in requests)
        return AnalysisResponse(candidates=candidates, usage=usage)

    return respond
