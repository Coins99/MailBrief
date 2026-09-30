"""Generate one consented daily brief: sync, bodies, analysis and the saved digest."""

import asyncio
import logging
from collections import Counter
from collections.abc import Callable
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
    emit_progress,
)
from mailbrief.services.application import ApplicationService
from mailbrief.services.bodies import BodyService
from mailbrief.services.calendar import day_window, local_day_window, resolve_timezone
from mailbrief.services.digest import DigestService
from mailbrief.services.history import check_brief_date
from mailbrief.services.proposals import ProposalService
from mailbrief.services.ranking import MAX_SHORTLIST_SIZE
from mailbrief.services.ranking import ShortlistGate as ShortlistGate
from mailbrief.storage.repositories import ConsentRepository
from mailbrief.storage.tables import AccountTable

logger = logging.getLogger(__name__)

CONSENT_DISCLOSURE_VERSION: Final = "2"
_DISPLAY_NAMES: Final = {"openai": "OpenAI", "groq": "Groq"}


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
    ) -> BriefRunResult:
        """Sync one day's Inbox, prepare bodies, confirm consent, analyze and save the brief.

        The day is today in ``tz_key``'s zone, or ``local_date``: today or one of the
        previous seven days. Any other date raises BriefDateError before Gmail is contacted
        or anything is written. A past day's brief covers only the messages still in the
        Inbox now. Messages from ``excluded_senders`` are never selected, downloaded or
        sent. Once the brief is saved, its follow-up signals become proposals for the
        owner's actions (ADR 0016); a failure there never fails the saved brief.
        """
        now = self._clock()
        zone = resolve_timezone(tz_key)
        today = local_day_window(now, zone).local_date
        check_brief_date(local_date or today, today)
        window = day_window(local_date or today, zone)
        account = await self._application.get_or_restore_account()
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
        )
        if sync.status is SyncStatus.CANCELLED:
            return BriefRunResult(status=BriefStatus.CANCELLED, sync=sync)
        if sync.status is SyncStatus.FAILED:
            return BriefRunResult(
                status=BriefStatus.SYNC_FAILED, sync=sync, error_code=sync.error_code
            )

        if cancel is not None and cancel.is_set():
            return BriefRunResult(status=BriefStatus.CANCELLED, sync=sync)
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
        if plan.to_send and not await self._analysis.credentials_available():
            # Nothing can be sent, so there is nothing to consent to; cached and skipped
            # messages still make a brief.
            run = self._analysis.fail_unsent(plan, KEY_MISSING)
        else:
            if plan.to_send and not await self._consented(account.id, plan, now):
                return BriefRunResult(status=BriefStatus.CONSENT_DECLINED, sync=sync)
            run = await self._analysis.execute(plan, cancel=cancel, progress=progress)
            if run.cancelled:
                return BriefRunResult(
                    status=BriefStatus.CANCELLED, sync=sync, ai_calls=run.requests_sent
                )

        coverage = self._coverage(sync, len(shortlist), run)
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
        )

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
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            ai_provider=self._analysis.provider_name if used else None,
            ai_model=self._analysis.model_name if used else None,
        )
