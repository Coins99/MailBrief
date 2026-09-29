"""Write a draft with an AI provider from context the owner chooses (ADR 0013).

Only the chosen parts are sent, after the owner approves a preview; the first use also
needs recorded consent. The source email's body is downloaded when a generation starts and
is never stored. The owner's text is saved as a version before the call, and a failed,
declined or cancelled generation leaves it unchanged. Errors and logs carry codes only.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.bodies import BodyStatus
from mailbrief.domain.common import normalize_utc
from mailbrief.domain.drafting import (
    ACTION_NOTES_SENT_CHARS,
    ACTION_STEPS_SENT_CHARS,
    COPIED_RUN_LIMIT,
    CURRENT_TEXT_SENT_CHARS,
    GENERATED_BODY_MAX_CHARS,
    GENERATED_SUBJECT_MAX_CHARS,
    MAX_MISSING_CONTEXT,
    MISSING_CONTEXT_MAX_CHARS,
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
    DraftingStatus,
    GeneratedDraft,
    PreviewLine,
    SourceContext,
    part_sizes,
)
from mailbrief.domain.drafts import Draft, DraftKind
from mailbrief.domain.messages import ProviderKind, RankedMessage
from mailbrief.ports.drafting import DraftingProvider
from mailbrief.ports.errors import ProviderError
from mailbrief.services.actions import ActionService
from mailbrief.services.analysis import KEY_MISSING, provider_detail, provider_error_code
from mailbrief.services.bodies import BodyService
from mailbrief.services.brief import provider_display_name
from mailbrief.services.drafts import DraftConflictError, DraftNotFoundError, DraftService
from mailbrief.storage.drafts import DraftRepository
from mailbrief.storage.repositories import (
    AccountRepository,
    MessageRepository,
    OwnerConsentRepository,
)
from mailbrief.text.matching import copies_long_run
from mailbrief.text.prepare import (
    clean_generated_block,
    clean_generated_text,
    trim_quoted_history,
    truncate_at_boundary,
)

logger = logging.getLogger(__name__)

DRAFTING_SCOPE: Final = "drafting"
DRAFTING_DISCLOSURE_VERSION: Final = "2"
OUTPUT_INCOMPLETE: Final = "AI_OUTPUT_INCOMPLETE"
INVALID_OUTPUT: Final = "AI_INVALID_OUTPUT"
REFUSED: Final = "AI_REFUSED"
DRAFT_CHANGED: Final = "DRAFT_CHANGED"
_ATTEMPTS: Final = 2  # An unusable answer is tried once more.
_SUBJECT_KINDS: Final = frozenset({DraftKind.EMAIL, DraftKind.NOTE})
_PART_LABELS: Final = {
    DraftContextPart.SOURCE_EMAIL: "The email: subject, sender name, received time and body",
    DraftContextPart.ACTION: "The linked action: title, whose, target, deadline, steps, notes",
    DraftContextPart.CURRENT_TEXT: "Your current text: title and body",
}
_ALWAYS_LABEL: Final = "Always: kind, tone, length, today's date and your instructions"
_PROBLEM_CODES: Final = {
    DraftingProblem.INCOMPLETE: OUTPUT_INCOMPLETE,
    DraftingProblem.INVALID_OUTPUT: INVALID_OUTPUT,
    DraftingProblem.REFUSED: REFUSED,
}


class DraftingContextError(LookupError):
    """A chosen part can't be sent now; the message is static."""


class DraftingGate(Protocol):
    """Asks the owner to approve one generation; the preview says whether it is first use."""

    async def request_drafting_consent(self, preview: DraftingPreview) -> bool: ...


@dataclass(frozen=True, slots=True)
class DraftingPlan:
    """One prepared generation: exactly what would be sent, and the draft revision it read."""

    public_id: str
    revision: int
    kind: DraftKind
    request: DraftingRequest = field(repr=False)
    preview: DraftingPreview


def _utc_now() -> datetime:
    return datetime.now(UTC)


def disclosure_lines(preview: DraftingPreview) -> tuple[str, ...]:
    """Plain sentences describing a generation, shared by the CLI and the desktop."""
    provider = provider_display_name(preview.provider)
    lines = [
        f"MailBrief will send these parts to {provider} ({preview.model}) to write this draft:",
        *(f"- {line.label}: {line.characters:,} characters" for line in preview.lines),
        "It never sends the sender's address, To or Cc, other emails, attachments, your other "
        "drafts or actions, Gmail or MailBrief IDs, or credentials. The parts listed above are "
        "sent as written, so any addresses, links or numbers inside them are sent too.",
        "An email's body is downloaded from Gmail for this draft only and is never saved.",
        "Your own text is saved as a version first; the result becomes a new version.",
        preview.privacy_notice,
    ]
    if preview.first_use:
        lines.append(
            "Your consent to AI drafting is remembered until you revoke it (Settings, or "
            "mailbrief-gmail-diagnostic ai-consent revoke)."
        )
    return tuple(lines)


def _bounded(text: str, limit: int) -> str:
    return truncate_at_boundary(text, limit)[0]


def validate_generated(
    candidate: DraftCandidate, request: DraftingRequest, kind: DraftKind
) -> GeneratedDraft:
    """Clean and check an untrusted answer; raises ValueError with a static message.

    The subject is kept only for emails and notes. The body loses control characters and
    quoted history, must be 1 to 8,000 characters, and must not copy 200 or more characters
    of the source email. At most five missing-context items of 200 characters are kept.
    """
    subject = None
    if kind in _SUBJECT_KINDS and candidate.subject is not None:
        subject = _bounded(clean_generated_text(candidate.subject), GENERATED_SUBJECT_MAX_CHARS)
    body, _ = trim_quoted_history(clean_generated_block(candidate.body))
    body = body.strip("\n")
    if not body.strip() or len(body) > GENERATED_BODY_MAX_CHARS:
        raise ValueError("the generated body is empty or too long")
    source = request.source
    if source is not None and copies_long_run(body, source.body, COPIED_RUN_LIMIT):
        raise ValueError("the generated body copies the email")
    missing = [
        _bounded(cleaned, MISSING_CONTEXT_MAX_CHARS)
        for item in candidate.missing_context
        if (cleaned := clean_generated_text(item))
    ]
    return GeneratedDraft(
        subject=subject or None, body=body, missing_context=tuple(missing[:MAX_MISSING_CONTEXT])
    )


class DraftingService:
    """Prepare and run one AI generation for a draft."""

    def __init__(
        self,
        session: AsyncSession,
        provider: DraftingProvider,
        bodies: BodyService | None = None,
        *,
        clock: Callable[[], datetime] = _utc_now,
        zone: ZoneInfo,
    ) -> None:
        self._session = session
        self._provider = provider
        self._bodies = bodies
        self._clock = clock
        self._zone = zone
        self._drafts = DraftService(session, clock=clock)
        self._consents = OwnerConsentRepository(session)

    async def available_parts(self, public_id: str) -> frozenset[DraftContextPart]:
        """What the owner may send now for this draft."""
        draft = await self._drafts.get(public_id)
        parts: set[DraftContextPart] = set()
        if self._bodies is not None and any(source.available for source in draft.sources):
            parts.add(DraftContextPart.SOURCE_EMAIL)
        if draft.action_public_id is not None:
            parts.add(DraftContextPart.ACTION)
        if draft.title.strip() or draft.body.strip():
            parts.add(DraftContextPart.CURRENT_TEXT)
        return frozenset(parts)

    async def consent_active(self) -> bool:
        consent = await self._consents.get_active(
            self._provider.provider_name, DRAFTING_SCOPE, DRAFTING_DISCLOSURE_VERSION
        )
        return consent is not None

    async def prepare(self, public_id: str, options: DraftingOptions) -> DraftingPlan:
        """Build exactly what would be sent and its preview; nothing is sent or stored.

        Choosing the email downloads its body now, prepared like the brief's.
        """
        draft = await self._drafts.get(public_id)
        if not options.parts <= await self.available_parts(public_id):
            raise DraftingContextError("That context is no longer available; choose again.")
        source = (
            await self._source(draft) if DraftContextPart.SOURCE_EMAIL in options.parts else None
        )
        action, action_cut = (
            await self._action(draft) if DraftContextPart.ACTION in options.parts else (None, False)
        )
        current = (
            CurrentText(title=draft.title, body=_bounded(draft.body, CURRENT_TEXT_SENT_CHARS))
            if DraftContextPart.CURRENT_TEXT in options.parts
            else None
        )
        request = DraftingRequest(
            kind=draft.kind,
            tone=options.tone,
            length=options.length,
            instructions=options.instructions,
            today=normalize_utc(self._clock()).astimezone(self._zone).date(),
            source=source,
            action=action,
            current=current,
        )
        sizes = part_sizes(request)
        cut = {
            DraftContextPart.SOURCE_EMAIL: source is not None and source.body_truncated,
            DraftContextPart.ACTION: action_cut,
            DraftContextPart.CURRENT_TEXT: current is not None
            and len(current.body) < len(draft.body),
        }
        lines = [PreviewLine(label=_ALWAYS_LABEL, characters=sizes[None])]
        lines.extend(
            PreviewLine(
                label=label + (" (cut to fit)" if cut.get(part) else ""),
                characters=sizes[part],
            )
            for part, label in _PART_LABELS.items()
            if part in sizes
        )
        preview = DraftingPreview(
            provider=self._provider.provider_name,
            model=self._provider.model_name,
            privacy_notice=self._provider.privacy_notice,
            first_use=not await self.consent_active(),
            lines=tuple(lines),
        )
        return DraftingPlan(
            public_id=public_id,
            revision=draft.revision,
            kind=draft.kind,
            request=request,
            preview=preview,
        )

    async def _source(self, draft: Draft) -> SourceContext:
        """The first source still in local mail, with its body downloaded now.

        available_parts() has checked that there is one and that bodies can be downloaded.
        """
        assert self._bodies is not None
        drafts = DraftRepository(self._session)
        row = await drafts.get_draft(draft.public_id)
        rows = [] if row is None else await drafts.source_rows(row.id)
        message_id = next((row.message_id for row in rows if row.message_id is not None), None)
        messages = MessageRepository(self._session)
        message = None if message_id is None else await messages.get_by_id(message_id)
        account = (
            None
            if message is None
            else await AccountRepository(self._session).get_by_id(message.account_id)
        )
        assert message is not None and account is not None
        normalized = MessageRepository.to_domain(
            message, account.provider_account_id, ProviderKind(account.provider)
        )
        (prepared,) = await self._bodies.prepare([RankedMessage(message=normalized, score=0)])
        if prepared.status is not BodyStatus.READY:
            raise DraftingContextError("That email's text couldn't be downloaded.")
        received = normalize_utc(message.received_at_utc).astimezone(self._zone)
        return SourceContext(
            subject=message.subject,
            sender_name=message.sender_name,
            received_local=received.isoformat(timespec="minutes"),
            body=prepared.text,
            body_truncated=prepared.truncated,
        )

    async def _action(self, draft: Draft) -> tuple[ActionContext, bool]:
        """The linked action, which available_parts() has checked is live; True if cut to fit.

        Steps are sent in order while they fit in ACTION_STEPS_SENT_CHARS, and notes are cut
        at ACTION_NOTES_SENT_CHARS.
        """
        assert draft.action_public_id is not None
        action = await ActionService(self._session).get(draft.action_public_id)
        steps: list[str] = []
        total = 0
        for step in action.steps:
            if total + len(step.text) > ACTION_STEPS_SENT_CHARS:
                break
            steps.append(step.text)
            total += len(step.text)
        notes, notes_cut = truncate_at_boundary(action.notes, ACTION_NOTES_SENT_CHARS)
        context = ActionContext(
            title=action.title,
            ownership=action.ownership,
            target_date=action.target_date,
            deadline_text=action.deadline_text,
            steps=tuple(steps),
            notes=notes,
        )
        return context, notes_cut or len(steps) < len(action.steps)

    async def generate(
        self,
        plan: DraftingPlan,
        gate: DraftingGate,
        cancel: asyncio.Event | None = None,
    ) -> DraftingOutcome:
        """Ask approval, save the owner's text as a version, call the provider and apply.

        Nothing is sent before approval, and a first-use grant is recorded before the call.
        A changed draft stops before anything is sent. An unusable answer is tried once
        more. After a failure, a decline or a cancel, the draft's text is unchanged.
        """

        def cancelled() -> bool:
            return cancel is not None and cancel.is_set()

        if cancelled():
            return DraftingOutcome(status=DraftingStatus.CANCELLED)
        if not await self._provider.credentials_available():
            return DraftingOutcome(status=DraftingStatus.FAILED, error_code=KEY_MISSING)
        first_use = not await self.consent_active()
        preview = plan.preview.model_copy(update={"first_use": first_use})
        if not await gate.request_drafting_consent(preview):
            logger.info("AI drafting declined")
            return DraftingOutcome(status=DraftingStatus.DECLINED)
        if cancelled():
            return DraftingOutcome(status=DraftingStatus.CANCELLED)
        if first_use:
            await self._consents.grant(
                self._provider.provider_name,
                DRAFTING_SCOPE,
                DRAFTING_DISCLOSURE_VERSION,
                normalize_utc(self._clock()),
            )
            await self._session.commit()
        try:
            previous = await self._drafts.keep_own_text(plan.public_id, plan.revision)
        except (DraftConflictError, DraftNotFoundError):
            logger.info("AI drafting stopped: the draft changed")
            return DraftingOutcome(status=DraftingStatus.FAILED, error_code=DRAFT_CHANGED)
        generated: GeneratedDraft | None = None
        problem: DraftingProblem | None = None
        for _ in range(_ATTEMPTS):
            if cancelled():
                return DraftingOutcome(status=DraftingStatus.CANCELLED)
            try:
                response = await self._provider.draft(plan.request)
            except ProviderError as exc:
                code = provider_error_code(exc)
                logger.info("AI drafting call failed: %s", code)
                return DraftingOutcome(
                    status=DraftingStatus.FAILED,
                    error_code=code,
                    provider_detail=provider_detail(exc),
                )
            if response.candidate is None:
                problem = response.problem
                continue
            try:
                generated = validate_generated(response.candidate, plan.request, plan.kind)
            except ValueError:
                problem = DraftingProblem.INVALID_OUTPUT
                continue
            break
        if cancelled():
            return DraftingOutcome(status=DraftingStatus.CANCELLED)
        if generated is None:
            code = _PROBLEM_CODES[problem or DraftingProblem.INVALID_OUTPUT]
            logger.info("AI drafting answer unusable: %s", code)
            return DraftingOutcome(status=DraftingStatus.FAILED, error_code=code)
        info = DraftGenerationInfo(
            provider=self._provider.provider_name,
            model=self._provider.model_name,
            prompt_version=self._provider.drafting_prompt_version,
            tone=plan.request.tone,
            length=plan.request.length,
            parts=plan.request.parts,
            instructions=plan.request.instructions,
            missing_context=generated.missing_context,
            created_at_utc=normalize_utc(self._clock()),
        )
        try:
            draft, number = await self._drafts.apply_generated(
                plan.public_id, plan.revision, generated, info
            )
        except (DraftConflictError, DraftNotFoundError):
            logger.info("AI drafting not applied: the draft changed")
            return DraftingOutcome(status=DraftingStatus.FAILED, error_code=DRAFT_CHANGED)
        return DraftingOutcome(
            status=DraftingStatus.GENERATED,
            draft=draft,
            version_number=number,
            previous_version=previous,
            missing_context=generated.missing_context,
        )
