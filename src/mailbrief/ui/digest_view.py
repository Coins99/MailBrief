"""Saved brief rendering: escaped content, Gmail sources and suggestion decisions."""

from datetime import UTC, date, datetime
from html import escape
from zoneinfo import ZoneInfo

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QTextBrowser

from mailbrief.domain.actions import TARGET_REASON_TEXT, SuggestionState, SuggestionView
from mailbrief.domain.analysis import ActionOwnership, DeadlinePrecision
from mailbrief.domain.digests import DailyDigest
from mailbrief.ui.deadline_text import deadline_text

ACCEPT = "accept"
DISMISS = "dismiss"


def _suggestion_html(view: SuggestionView, accept: str, dismiss: str, zone: ZoneInfo) -> str:
    """One pending suggestion; every mail- or AI-derived string is escaped.

    An exact deadline is shown in ``zone``, the brief's, like the message's own deadline.
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
    return (
        f"<p>Suggested: {escape(suggestion.title)} ({escape('; '.join(detail))})<br>"
        f'<a href="{accept}">Accept</a> · <a href="{dismiss}">Dismiss</a></p>'
        + (f"<ul>{steps}</ul>" if steps else "")
    )


class DigestView(QTextBrowser):
    """Suggestion links only emit ``suggestion_requested(kind, suggestion_id)``, and reply
    links ``reply_requested(account_email, message_id)``; the window decides what happens.
    Unknown links, including any an email could smuggle in, do nothing.
    """

    suggestion_requested = Signal(str, int)
    reply_requested = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self.setAccessibleName("Saved daily brief")
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.anchorClicked.connect(self._open_source)
        self._sources: dict[str, str] = {}
        self._suggestions: dict[str, tuple[str, int]] = {}
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
        reply = self._replies.get(key)
        if reply is not None:
            self.reply_requested.emit(*reply)

    def show_digest(self, digest: DailyDigest) -> None:
        """Render a brief. Re-rendering the same brief, as after Accept or Dismiss, keeps the
        reader's place; a different brief starts at the top."""
        identity = (digest.account_id, digest.local_date, digest.generated_at_utc)
        same = identity == self._shown
        position = self.verticalScrollBar().value()
        self._shown = identity
        self._sources.clear()
        self._suggestions.clear()
        self._replies.clear()
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
        ]
        coverage = digest.coverage
        if coverage is not None:
            parts.append(
                f"<p>{coverage.analyzed} analyzed · {coverage.reused} reused · "
                f"{coverage.failed} failed · {coverage.skipped} skipped · "
                f"Inbox sync {'complete' if coverage.sync_complete else 'incomplete'}</p>"
            )
        if not digest.items:
            parts.append("<p>No analyzed messages in this brief.</p>")
        section = ""
        for index, item in enumerate(digest.items):
            if item.section.value != section:
                section = item.section.value
                parts.append(f"<h3>{escape(section.title())}</h3>")
            parts.append(
                f"<p><b>{escape(item.subject)}</b><br>{escape(item.sender.address)}<br>"
                f"{escape(item.summary)}</p>"
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
                    parts.append(
                        _suggestion_html(view, accept, dismiss, ZoneInfo(digest.timezone_name))
                    )
            # The brief's account_id is the account's email address, and each item's
            # message_key is its Gmail message ID.
            reply = f"mailbrief:reply/{index}"
            self._replies[reply] = (digest.account_id, item.message_key)
            links = [f'<a href="{reply}">Draft a reply</a>']
            source = item.source_url
            if source.scheme == "https" and source.host == "mail.google.com":
                link = f"mailbrief:source/{index}"
                self._sources[link] = str(source)
                links.append(f'<a href="{link}">Open source in Gmail</a>')
            parts.append(f"<p>{' · '.join(links)}</p>")
        self.setHtml("".join(parts))
        self.verticalScrollBar().setValue(position if same else 0)
