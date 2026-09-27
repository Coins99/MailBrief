"""Saved brief rendering: escaped content and explicitly activated Gmail sources."""

from datetime import UTC, datetime
from html import escape
from zoneinfo import ZoneInfo

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QTextBrowser

from mailbrief.domain.analysis import DeadlinePrecision
from mailbrief.domain.digests import DailyDigest


class DigestView(QTextBrowser):
    def __init__(self) -> None:
        super().__init__()
        self.setAccessibleName("Saved daily brief")
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.anchorClicked.connect(self._open_source)
        self._sources: dict[str, str] = {}
        self.setPlainText("No saved brief yet. Connect Gmail, then sync and review your shortlist.")

    def _open_source(self, url: QUrl) -> None:
        target = self._sources.get(url.toString())
        if target is not None:
            QDesktopServices.openUrl(QUrl(target))

    def show_digest(self, digest: DailyDigest) -> None:
        self._sources.clear()
        age = max(0, int((datetime.now(UTC) - digest.generated_at_utc).total_seconds() // 60))
        count, unit = (age, "minute") if age < 60 else (age // 60, "hour")
        if age >= 1440:
            count, unit = age // 1440, "day"
        age_label = f"{count} {unit}{'' if count == 1 else 's'}"
        parts = [
            f"<h2>{digest.local_date.isoformat()} · {escape(digest.status.value.title())}</h2>",
            f"<p>{escape(digest.account_id)} · {escape(digest.timezone_name)}<br>"
            f"Saved {escape(digest.generated_at_utc.astimezone().isoformat(timespec='minutes'))} "
            f"({age_label} ago at load)</p>",
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
            source = item.source_url
            if source.scheme == "https" and source.host == "mail.google.com":
                link = f"mailbrief:source/{index}"
                self._sources[link] = str(source)
                parts.append(f'<p><a href="{link}">Open source in Gmail</a></p>')
        self.setHtml("".join(parts))
