"""Generate one consented daily brief: sync, bodies, analysis and the saved digest."""

import asyncio
import logging
from collections import Counter
from collections.abc import Callable, Collection, Iterable
from datetime import UTC, date, datetime
from typing import Final, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.analysis import AIUsage
from mailbrief.domain.briefs import (
    AnalysisOutcome,
    BriefRunResult,
    BriefStatus,
    TransmissionPreview,
)
from mailbrief.domain.digests import DigestCoverage, SyncProgress, SyncResult, SyncStage, SyncStatus
from mailbrief.services.analysis import (
    KEY_MISSING,
    AnalysisPlan,
    AnalysisRun,
    AnalysisService,
    PlannedMessage,
    emit_progress,
)
from mailbrief.services.application import ApplicationService
from mailbrief.services.bodies import BodyService
from mailbrief.services.calendar import day_window, local_day_window, resolve_timezone
from mailbrief.services.digest import IN_BRIEF, DigestService
from mailbrief.services.history import BriefDateError, check_brief_date
from mailbrief.services.proposals import ProposalService
from mailbrief.services.ranking import MAX_SHORTLIST_SIZE
from mailbrief.services.ranking import ShortlistGate as ShortlistGate
from mailbrief.storage.repositories import ConsentRepository
from mailbrief.storage.tables import AccountTable

logger = logging.getLogger(__name__)

CONSENT_DISCLOSURE_VERSION: Final = "2"
_DISPLAY_NAMES: Final = {"openai": "OpenAI", "groq": "Groq"}
_AUTOMATIC_TODAY: Final = "Automatic runs brief today only."


class ConsentGate(Protocol):
    """Asks the user whether the previewed transmission may happen."""

    async def confirm(self, preview: TransmissionPreview) -> bool: ...


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def provider_display_name(name: str) -> str:
    """The provider's name as people write it; unknown names pass through unchanged."""
    return _DISPLAY_NAMES.get(name, name)


def disclosure_lines(preview: TransmissionPreview) -> tuple[str, ...]:
    """Plain sentences describing what will be sent, shared by the CLI and the UI."""
    provider = provider_display_name(preview.provider_name)
    cut = preview.truncated_count
    lines = [
        f"MailBrief will send {_count(preview.message_count, 'message')} to {provider} "
        f"({preview.model_name}) for analysis.",
        f"{_count(cut, 'message')} {'is' if cut == 1 else 'are'} cut to fit the length limit."
        if cut
        else "No message is cut to fit the length limit.",
        "For each message it sends: " + "; ".join(preview.fields) + ".",
        "It never sends attachments, recipients, message IDs, links, account IDs or credentials.",
        preview.privacy_notice,
    ]
    if preview.reused_count:
        lines.append(
            f"{_count(preview.reused_count, 'message')} already analyzed will not be sent again."
        )
    if preview.first_use:
        lines.append("Your consent is remembered for this account until you revoke it.")
    return tuple(lines)


def permission_preview(
    limit: int,
    *,
    provider_name: str,
    model_name: str,
    body_character_limit: int,
    privacy_notice: str,
) -> TransmissionPreview:
    """What an automatic run with permission for ``limit`` messages (at least 1) may send,
    as the transmission preview the disclosure lines describe (ADR 0017)."""
    return TransmissionPreview(
        provider_name=provider_name,
        model_name=model_name,
        message_count=limit,
        truncated_count=0,
        reused_count=0,
        first_use=False,
        body_character_limit=body_character_limit,
        privacy_notice=privacy_notice,
    )


def permission_sentence(limit: int, account_email: str, provider_name: str) -> str:
    """The plain sentence that says what the automatic-analysis permission allows."""
    noun = "message" if limit == 1 else "messages"
    return (
        f"Automatic runs may send up to {limit} {noun} from {account_email} to "
        f"{provider_display_name(provider_name)} without asking. "
        "Revoke consent in Settings to stop."
    )


def needs_review_sentence(count: int) -> str:
    """What an automatic run says when it saved nothing rather than lose a carried message
    (ADR 0017): how many of the day's brief's messages it didn't refresh, because they were
    over its send limit, unreadable or failed in analysis. Shared by the CLI and the UI."""
    return (
        f"Today's brief needs your review: {_count(count, 'message')} from it couldn't be "
        "refreshed automatically."
    )


def _not_refreshed(
    messages: Iterable[PlannedMessage], carried: Collection[str]
) -> list[PlannedMessage]:
    """The carried messages that a brief saved from ``messages`` would leave out."""
    return [
        item
        for item in messages
        if item.outcome not in IN_BRIEF and item.ranked.message.provider_message_id in carried
    ]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class BriefService:
    """Generate and save one local day's brief, asking consent before anything is sent."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        application: ApplicationService,
        bodies: BodyService,
        analysis: AnalysisService,
        digests: DigestService,
        consent_gate: ConsentGate,
        clock: Callable[[], datetime] = _utc_now,
        proposals: ProposalService | None = None,
    ) -> None:
        self._session = session
        self._application = application
        self._bodies = bodies
        self._analysis = analysis
        self._digests = digests
        self._consent_gate = consent_gate
        self._clock = clock
        self._consents = ConsentRepository(session)
        self._proposals = proposals or ProposalService(session, clock=clock)

    async def generate(
        self,
        *,
        tz_key: str | None = None,
        include_ids: tuple[str, ...] = (),
        exclude_ids: tuple[str, ...] = (),
        cancel: asyncio.Event | None = None,
        progress: Callable[[SyncProgress], None] | None = None,
        shortlist_gate: ShortlistGate | None = None,
        shortlist_limit: int = MAX_SHORTLIST_SIZE,
        excluded_senders: tuple[str, ...] = (),
        local_date: date | None = None,
        automatic: bool = False,
    ) -> BriefRunResult:
        """Sync one day's Inbox, prepare bodies, confirm consent, analyze and save the brief.

        The day is today in ``tz_key``'s zone, or ``local_date``: today or one of the
        previous seven days. Any other date raises BriefDateError before Gmail is contacted
        or anything is written. A past day's brief covers only the messages still in the
        Inbox now. Messages from ``excluded_senders`` are never selected, downloaded or
        sent. Once the brief is saved, its follow-up signals become proposals for the
        owner's actions (ADR 0016); a failure there never fails the saved brief.

        Runs through a day are cumulative: the messages of the day's saved brief are carried
        forward, so a later run, automatic or not, never drops one the owner chose unless it
        is archived, newly blocked or declined; the day still has one brief.

        An ``automatic`` run (ADR 0017) briefs today only, takes no review and never asks
        for consent. It syncs, checks threads and ranks, then reads the automatic-analysis
        permission on the active consent for the current disclosure version. Without one, or
        with nothing new selected, it returns READY_FOR_REVIEW with the number of new
        messages (the selection minus the carried ones) that are ready, before any body is
        downloaded and with nothing sent or saved. With permission it downloads the bodies
        and sends at most min(permission, ``shortlist_limit``) messages: the carried ones
        that need analysis again first, then the others, each in rank order. It defers the
        rest (never sent, never cached, counted in the coverage), then saves the brief and
        derives proposals as usual. It never saves a brief that would lose a carried message.
        When one is over that cap, or its body can't be read, the run sends and saves
        nothing; when one's analysis fails, it writes no brief and leaves the analyses that
        succeeded cached. Either way it returns READY_FOR_REVIEW with ``needs_review`` and
        ``ready`` set to the number of carried messages it didn't refresh.
        """
        if automatic and (shortlist_gate is not None or include_ids or exclude_ids):
            raise ValueError("An automatic run has no review.")
        now = self._clock()
        zone = resolve_timezone(tz_key)
        today = local_day_window(now, zone).local_date
        if automatic and local_date not in (None, today):
            raise BriefDateError(_AUTOMATIC_TODAY)
        check_brief_date(local_date or today, today)
        window = day_window(local_date or today, zone)
        account = await self._application.get_or_restore_account()
        # Runs through a day are cumulative: the day's saved brief is carried forward.
        carried, carried_outside = await self._digests.carried(account.id, window.local_date)
        sync, shortlist = await self._application.prepare_daily_shortlist(
            tz_key=window.timezone_name,
            now_utc=now,
            include_ids=include_ids,
            exclude_ids=exclude_ids,
            progress=progress,
            cancel=cancel,
            shortlist_gate=shortlist_gate,
            shortlist_limit=shortlist_limit,
            excluded_senders=excluded_senders,
            local_date=window.local_date,
            carried=carried,
            carried_outside=carried_outside,
        )
        if sync.status is SyncStatus.CANCELLED:
            return BriefRunResult(status=BriefStatus.CANCELLED, sync=sync)
        if sync.status is SyncStatus.FAILED:
            return BriefRunResult(
                status=BriefStatus.SYNC_FAILED, sync=sync, error_code=sync.error_code
            )

        if cancel is not None and cancel.is_set():
            return BriefRunResult(status=BriefStatus.CANCELLED, sync=sync)
        send_limit = await self._auto_send_limit(account.id) if automatic else 0
        new = sum(item.message.provider_message_id not in carried for item in shortlist)
        if automatic and (send_limit == 0 or new == 0):
            # Nothing may be sent, or nothing is new: report, and leave the saved brief.
            logger.info("Automatic run: %d new messages ready, nothing sent", new)
            return BriefRunResult(status=BriefStatus.READY_FOR_REVIEW, sync=sync, ready=new)
        prepared = await self._bodies.prepare(shortlist)
        plan = await self._analysis.plan(
            account_id=account.id,
            shortlist=shortlist,
            bodies=prepared,
            timezone_name=window.timezone_name,
        )
        if cancel is not None and cancel.is_set():
            # Before the key check and consent, so a cancelled run writes no brief.
            return BriefRunResult(status=BriefStatus.CANCELLED, sync=sync)
        if automatic:
            # Carried messages that need analysis again take the places first. The shortlist
            # is sorted by rank, so the lowest-ranked of the others wait.
            plan.defer_after(min(send_limit, shortlist_limit), first=carried)
            stale = _not_refreshed(plan.messages, carried)
            if any(item.outcome is not None for item in stale):
                # A carried message is over the cap, or its body failed or was skipped, so a
                # saved brief would lose it: nothing is sent or saved, and the day's brief
                # waits for the owner's review.
                logger.info(
                    "Automatic run: %d carried messages not refreshed, nothing sent", len(stale)
                )
                return BriefRunResult(
                    status=BriefStatus.READY_FOR_REVIEW,
                    sync=sync,
                    ready=len(stale),
                    needs_review=True,
                )
        if plan.to_send and not await self._analysis.credentials_available():
            # Nothing can be sent, so there is nothing to consent to; cached and skipped
            # messages still make a brief.
            run = self._analysis.fail_unsent(plan, KEY_MISSING)
        else:
            # An automatic run never asks: its permission is the owner's answer (ADR 0017).
            if not automatic and plan.to_send and not await self._consented(account.id, plan, now):
                return BriefRunResult(status=BriefStatus.CONSENT_DECLINED, sync=sync)
            run = await self._analysis.execute(plan, cancel=cancel, progress=progress)
            if run.cancelled:
                return BriefRunResult(
                    status=BriefStatus.CANCELLED, sync=sync, ai_calls=run.requests_sent
                )

        coverage = self._coverage(sync, len(shortlist), run)
        lost = _not_refreshed(run.messages, carried) if automatic else []
        if lost:
            # A carried message's analysis failed, so a saved brief would lose it: no brief
            # is written. The analyses that succeeded are cached already (execute commits
            # each call), so the owner's review reuses them.
            logger.info(
                "Automatic run: %d carried messages not refreshed, no brief saved", len(lost)
            )
            return BriefRunResult(
                status=BriefStatus.READY_FOR_REVIEW,
                sync=sync,
                coverage=coverage,
                error_code=run.error_code,
                ai_calls=run.requests_sent,
                provider_detail=run.provider_detail,
                deferred=coverage.deferred,
                ready=len(lost),
                needs_review=True,
            )
        emit_progress(progress, SyncProgress(stage=SyncStage.ASSEMBLING))
        digest = await self._digests.save(
            account_id=account.id,
            account_email=account.email_address,
            window=window,
            messages=run.messages,
            coverage=coverage,
            outside_ids=sync.outside_ids,
        )
        if digest is None:
            return BriefRunResult(
                status=BriefStatus.ANALYSIS_FAILED,
                sync=sync,
                coverage=coverage,
                error_code=run.error_code or "ANALYSIS_FAILED",
                ai_calls=run.requests_sent,
                provider_detail=run.provider_detail,
                deferred=coverage.deferred,
            )
        return BriefRunResult(
            status=BriefStatus.SAVED,
            sync=sync,
            digest=digest,
            coverage=coverage,
            error_code=run.error_code,
            ai_calls=run.requests_sent,
            provider_detail=run.provider_detail,
            proposals_created=await self._derive(account, run, window.timezone_name),
            deferred=coverage.deferred,
        )

    async def _auto_send_limit(self, account_id: int) -> int:
        """How many messages an automatic run may send without asking: the permission on the
        active consent for the current disclosure version, 0 when there is none."""
        consent = await self._consents.get_active(
            account_id, self._analysis.provider_name, CONSENT_DISCLOSURE_VERSION
        )
        return 0 if consent is None else consent.auto_send_limit

    async def _derive(self, account: AccountTable, run: AnalysisRun, timezone_name: str) -> int:
        """Proposals from the saved brief's analyses; a failure is logged by type only."""
        try:
            return await self._proposals.derive(account, run.messages, timezone_name)
        except Exception as exc:
            logger.warning("Follow-up proposals failed: %s", type(exc).__name__)
            return 0

    async def _consented(self, account_id: int, plan: AnalysisPlan, now: datetime) -> bool:
        """Ask the gate; record first-use consent before any provider call."""
        provider = self._analysis.provider_name
        active = await self._consents.get_active(account_id, provider, CONSENT_DISCLOSURE_VERSION)
        to_send = plan.to_send
        preview = TransmissionPreview(
            provider_name=provider,
            model_name=self._analysis.model_name,
            message_count=len(to_send),
            truncated_count=sum(
                item.request is not None and item.request.body_truncated for item in to_send
            ),
            reused_count=sum(item.outcome is AnalysisOutcome.REUSED for item in plan.messages),
            first_use=active is None,
            body_character_limit=self._bodies.limit,
            privacy_notice=self._analysis.privacy_notice,
        )
        if not await self._consent_gate.confirm(preview):
            logger.info("AI consent declined for %d messages", preview.message_count)
            return False
        if preview.first_use:
            await self._consents.grant(account_id, provider, CONSENT_DISCLOSURE_VERSION, now)
            await self._session.commit()
        return True

    def _coverage(self, sync: SyncResult, shortlisted: int, run: AnalysisRun) -> DigestCoverage:
        counts = Counter(item.outcome for item in run.messages)
        used = counts[AnalysisOutcome.ANALYZED] + counts[AnalysisOutcome.REUSED]
        usage = run.usage or AIUsage()
        return DigestCoverage(
            sync_complete=sync.status is SyncStatus.COMPLETE,
            shortlisted=shortlisted,
            analyzed=counts[AnalysisOutcome.ANALYZED],
            reused=counts[AnalysisOutcome.REUSED],
            failed=counts[AnalysisOutcome.FAILED],
            skipped=counts[AnalysisOutcome.SKIPPED],
            deferred=counts[AnalysisOutcome.DEFERRED],
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            ai_provider=self._analysis.provider_name if used else None,
            ai_model=self._analysis.model_name if used else None,
        )
