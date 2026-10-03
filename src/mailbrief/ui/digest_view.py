"""Saved brief rendering: escaped content, Gmail sources, suggestion decisions, the actions
that continue each email's thread, and the updates an email proposes to an action."""

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from html import escape
from zoneinfo import ZoneInfo

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QTextBrowser

from mailbrief.domain.actions import (
    TARGET_REASON_TEXT,
    ActionProposal,
    ProposalState,
    SuggestionState,
    SuggestionView,
    ThreadLink,
)
from mailbrief.domain.analysis import ActionOwnership, DeadlinePrecision
from mailbrief.domain.digests import SECTION_TITLES, DailyDigest
from mailbrief.services.history import coverage_line
from mailbrief.ui.deadline_text import deadline_text
from mailbrief.ui.proposals_view import effect_text

ACCEPT = "accept"
DISMISS = "dismiss"
APPLY = "apply"


def _owner(ownership: ActionOwnership) -> str:
    return "mine" if ownership is ActionOwnership.MINE else "waiting for"


def _suggestion_html(
    view: SuggestionView,
    accept: str,
    dismiss: str,
    zone: ZoneInfo,
    into: Sequence[tuple[str, str]] = (),
) -> str:
    """One pending suggestion; every mail- or AI-derived string is escaped.

    An exact deadline is shown in ``zone``, the brief's, like the message's own deadline.
    ``into`` holds an internal link and an action title for each "Add to" offer.
    """
    suggestion = view.suggestion
    owner = "yours" if suggestion.ownership is ActionOwnership.MINE else "waiting for someone"
    detail = [owner]
    if suggestion.suggested_target_date is not None and suggestion.target_reason is not None:
        detail.append(
            f"target {suggestion.suggested_target_date.isoformat()}, "
            f"{TARGET_REASON_TEXT[suggestion.target_reason]}"
        )
    due = deadline_text(suggestion, zone)
    if due is not None:
        detail.append(f"due {due}")
    steps = "".join(f"<li>{escape(step)}</li>" for step in suggestion.steps)
    links = [f'<a href="{accept}">Accept</a>', f'<a href="{dismiss}">Dismiss</a>']
    links.extend(f'<a href="{link}">Add to “{escape(title)}”</a>' for link, title in into)
    return (
        f"<p>Suggested: {escape(suggestion.title)} ({escape('; '.join(detail))})<br>"
        + " · ".join(links)
        + "</p>"
        + (f"<ul>{steps}</ul>" if steps else "")
    )


def _proposal_html(proposal: ActionProposal, apply: str, dismiss: str, zone: ZoneInfo) -> str:
    """One pending proposal: whom it is for, the email's quote, then what applying does.

    The quote, the action's title and the effect (which may hold the email's own words for a
    deadline) are all escaped. An exact deadline is shown in ``zone``, the brief's.
    """
    return (
        f"<p>Proposes for “{escape(proposal.action_title)}”: “{escape(proposal.evidence)}”<br>"
        f'<a href="{apply}">{escape(effect_text(proposal, zone))}</a> · '
        f'<a href="{dismiss}">Dismiss</a></p>'
    )


class DigestView(QTextBrowser):
    """Suggestion links only emit ``suggestion_requested(kind, suggestion_id)``, "Add to"
    links ``accept_into_requested(suggestion_id, public_id, revision)``, proposal links
    ``proposal_requested(kind, proposal_id, action_revision)`` with kind APPLY or DISMISS,
    and reply links ``reply_requested(account_email, message_id)``; the window decides what
    happens. Unknown links, including any an email could smuggle in, do nothing.
    """

    suggestion_requested = Signal(str, int)
    accept_into_requested = Signal(int, str, int)
    proposal_requested = Signal(str, int, int)
    reply_requested = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self.setAccessibleName("Saved daily brief")
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.anchorClicked.connect(self._open_source)
        self._sources: dict[str, str] = {}
        self._suggestions: dict[str, tuple[str, int]] = {}
        # "Add to" links: suggestion ID, action public ID and the revision shown.
        self._into: dict[str, tuple[int, str, int]] = {}
        # Proposal links: APPLY or DISMISS, the proposal ID and the action revision shown.
        self._proposals: dict[str, tuple[str, int, int]] = {}
        self._replies: dict[str, tuple[str, str]] = {}
        # The brief shown: account, local date and when it was generated.
        self._shown: tuple[str, date, datetime] | None = None
        # The owner's zone, for when the brief was saved; the window sets it. Deadlines
        # keep the brief's own zone.
        self.zone: ZoneInfo | None = None
        self.setPlainText("No saved brief yet. Connect Gmail, then sync and review your shortlist.")

    def _open_source(self, url: QUrl) -> None:
        key = url.toString()
        target = self._sources.get(key)
        if target is not None:
            QDesktopServices.openUrl(QUrl(target))
            return
        request = self._suggestions.get(key)
        if request is not None:
            self.suggestion_requested.emit(*request)
            return
        into = self._into.get(key)
        if into is not None:
            self.accept_into_requested.emit(*into)
            return
        proposal = self._proposals.get(key)
        if proposal is not None:
            self.proposal_requested.emit(*proposal)
            return
        reply = self._replies.get(key)
        if reply is not None:
            self.reply_requested.emit(*reply)

    def show_digest(
        self,
        digest: DailyDigest,
        links: Mapping[str, tuple[ThreadLink, ...]] | None = None,
        proposals: Mapping[str, tuple[ActionProposal, ...]] | None = None,
    ) -> None:
        """Render a brief. Re-rendering the same brief, as after Accept or Dismiss, keeps the
        reader's place; a different brief starts at the top.

        ``links`` maps an item's message key to the live, open actions that continue its
        thread: each is named on the item unless the email is already its source, and each
        pending suggestion offers to add the email to it. ``proposals`` maps an item's message
        key to the pending updates it proposes to actions: each is shown under the item, with
        a link named by its effect and a Dismiss link.
        """
        identity = (digest.account_id, digest.local_date, digest.generated_at_utc)
        same = identity == self._shown
        position = self.verticalScrollBar().value()
        self._shown = identity
        self._sources.clear()
        self._suggestions.clear()
        self._into.clear()
        self._proposals.clear()
        self._replies.clear()
        continued = links or {}
        proposed = proposals or {}
        age = max(0, int((datetime.now(UTC) - digest.generated_at_utc).total_seconds() // 60))
        count, unit = (age, "minute") if age < 60 else (age // 60, "hour")
        if age >= 1440:
            count, unit = age // 1440, "day"
        age_label = f"{count} {unit}{'' if count == 1 else 's'}"
        saved = digest.generated_at_utc.astimezone(self.zone).isoformat(timespec="minutes")
        parts = [
            f"<h2>{digest.local_date.isoformat()} · {escape(digest.status.value.title())}</h2>",
            f"<p>{escape(digest.account_id)} · {escape(digest.timezone_name)}<br>"
            f"Saved {escape(saved)} ({age_label} ago at load)</p>",
            f"<p>{escape(coverage_line(digest))}</p>",
        ]
        coverage = digest.coverage
        if coverage is not None:
            # Messages an automatic run left for your next review (ADR 0017), only when any.
            deferred = f"{coverage.deferred} deferred · " if coverage.deferred else ""
            parts.append(
                f"<p>{coverage.analyzed} analyzed · {coverage.reused} reused · "
                f"{coverage.failed} failed · {coverage.skipped} skipped · {deferred}"
                f"Inbox sync {'complete' if coverage.sync_complete else 'incomplete'}</p>"
            )
        if not digest.items:
            parts.append("<p>No analyzed messages in this brief.</p>")
        section = ""
        for index, item in enumerate(digest.items):
            if item.section.value != section:
                section = item.section.value
                parts.append(f"<h3>{escape(SECTION_TITLES[item.section])}</h3>")
            parts.append(
                f"<p><b>{escape(item.subject)}</b><br>{escape(item.sender.address)}<br>"
                f"{escape(item.summary)}</p>"
            )
            item_links = continued.get(item.message_key, ())
            for tracked in item_links:
                if not tracked.is_source:
                    parts.append(
                        f"<p>Continues: “{escape(tracked.title)}” ({_owner(tracked.ownership)})</p>"
                    )
            for text in (item.action_text, item.deadline_text):
                if text:
                    parts.append(f"<p>{escape(text)}</p>")
            if item.deadline_precision is DeadlinePrecision.DATE and item.deadline_date is not None:
                parts.append(f"<p>Due {item.deadline_date.isoformat()} (date only)</p>")
            elif (
                item.deadline_precision is DeadlinePrecision.DATETIME
                and item.deadline_at_utc is not None
            ):
                local = item.deadline_at_utc.astimezone(ZoneInfo(digest.timezone_name))
                parts.append(f"<p>Due {escape(local.isoformat(timespec='minutes'))}</p>")
            for view in item.suggestions:
                if view.state is SuggestionState.ACCEPTED:
                    parts.append(f"<p>Accepted: {escape(view.suggestion.title)}</p>")
                elif view.state is SuggestionState.PENDING:
                    accept = f"mailbrief:accept/{view.suggestion_id}"
                    dismiss = f"mailbrief:dismiss/{view.suggestion_id}"
                    self._suggestions[accept] = (ACCEPT, view.suggestion_id)
                    self._suggestions[dismiss] = (DISMISS, view.suggestion_id)
                    into: list[tuple[str, str]] = []
                    for tracked in item_links:
                        key = f"mailbrief:into/{len(self._into)}"
                        self._into[key] = (view.suggestion_id, tracked.public_id, tracked.revision)
                        into.append((key, tracked.title))
                    parts.append(
                        _suggestion_html(
                            view, accept, dismiss, ZoneInfo(digest.timezone_name), into
                        )
                    )
            for proposal in proposed.get(item.message_key, ()):
                if proposal.state is not ProposalState.PENDING:
                    continue
                apply = f"mailbrief:proposal/{len(self._proposals)}"
                self._proposals[apply] = (APPLY, proposal.id, proposal.action_revision)
                dismiss = f"mailbrief:proposal/{len(self._proposals)}"
                self._proposals[dismiss] = (DISMISS, proposal.id, proposal.action_revision)
                parts.append(
                    _proposal_html(proposal, apply, dismiss, ZoneInfo(digest.timezone_name))
                )
            # The brief's account_id is the account's email address, and each item's
            # message_key is its Gmail message ID.
            reply = f"mailbrief:reply/{index}"
            self._replies[reply] = (digest.account_id, item.message_key)
            footer = [f'<a href="{reply}">Draft a reply</a>']
            source = item.source_url
            if source.scheme == "https" and source.host == "mail.google.com":
                link = f"mailbrief:source/{index}"
                self._sources[link] = str(source)
                footer.append(f'<a href="{link}">Open source in Gmail</a>')
            parts.append(f"<p>{' · '.join(footer)}</p>")
        self.setHtml("".join(parts))
        self.verticalScrollBar().setValue(position if same else 0)
