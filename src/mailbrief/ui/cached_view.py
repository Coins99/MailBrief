"""Read-only cached mail browsing; no provider calls or body downloads."""

from datetime import date
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from mailbrief.domain.cached_mail import CachedAccount, CachedMailPage


class CachedMailDialog(QDialog):
    page_requested = Signal(int, object, int)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Saved mail metadata")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(850, 620)
        self._page: CachedMailPage | None = None
        # The owner's zone, for the last sync time; the window sets it.
        self.zone: ZoneInfo | None = None
        layout = QVBoxLayout(self)
        filters = QHBoxLayout()
        self.accounts = QComboBox()
        self.accounts.setAccessibleName("Cached Gmail account")
        self.day = QDateEdit(QDate.currentDate())
        self.day.setAccessibleName("Received date in your local timezone")
        self.day.setCalendarPopup(True)
        self.day.setDisplayFormat("yyyy-MM-dd")
        filters.addWidget(self.accounts, 1)
        filters.addWidget(self.day)
        layout.addLayout(filters)
        self.status = QLabel("Choose a cached account and date.")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.messages = QListWidget()
        self.messages.setAccessibleName("Cached message metadata")
        layout.addWidget(self.messages, 1)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setAccessibleName("Selected cached message details")
        layout.addWidget(self.details, 1)
        navigation = QHBoxLayout()
        self.previous = QPushButton("&Previous 100")
        self.next = QPushButton("&Next 100")
        self.source = QPushButton("Open source in &Gmail")
        close = QPushButton("&Close")
        for button in (self.previous, self.next, self.source, close):
            navigation.addWidget(button)
        layout.addLayout(navigation)
        close.clicked.connect(self.reject)
        self.accounts.currentIndexChanged.connect(lambda: self.request_page(0))
        self.day.dateChanged.connect(lambda: self.request_page(0))
        self.previous.clicked.connect(
            lambda: self.request_page(max(0, self._page.offset - 100) if self._page else 0)
        )
        self.next.clicked.connect(
            lambda: self.request_page(self._page.offset + 100 if self._page else 0)
        )
        self.messages.currentRowChanged.connect(self._show_message)
        self.source.clicked.connect(self._open_source)
        self.set_busy(False)

    def configure(self, accounts: tuple[CachedAccount, ...]) -> None:
        self.accounts.blockSignals(True)
        self.accounts.clear()
        for account in accounts:
            self.accounts.addItem(account.email_address, account.account_id)
        self.accounts.blockSignals(False)
        self.begin_load()
        if not accounts:
            self.status.setText("No cached Gmail accounts yet. Connect and sync to save metadata.")

    @property
    def has_page(self) -> bool:
        return self._page is not None

    def selection(self) -> tuple[int, date] | None:
        identifier = self.accounts.currentData()
        if not isinstance(identifier, int):
            return None
        value = self.day.date()
        return identifier, date(value.year(), value.month(), value.day())

    def request_page(self, offset: int) -> None:
        selected = self.selection()
        if selected is not None:
            self.page_requested.emit(selected[0], selected[1], offset)

    def begin_load(self) -> None:
        self._page = None
        self.messages.clear()
        self.details.clear()
        self.status.setText("Loading saved metadata…")
        self.source.setEnabled(False)

    def set_busy(self, busy: bool) -> None:
        self.accounts.setEnabled(not busy)
        self.day.setEnabled(not busy)
        self.previous.setEnabled(not busy and self._page is not None and self._page.offset > 0)
        self.next.setEnabled(not busy and self._page is not None and self._page.has_more)

    def show_page(self, page: CachedMailPage) -> None:
        self._page = page
        last = page.account.last_sync_at_utc
        stamp = (
            last.astimezone(self.zone).isoformat(timespec="minutes") if last else "none recorded"
        )
        self.status.setText(
            f"{page.local_date} ({page.timezone_name}) · {len(page.messages)} saved messages "
            f"on this page. Account last complete sync: {stamp}. "
            "Cached metadata may be stale; browsing does not use the network."
        )
        self.messages.clear()
        for message in page.messages:
            self.messages.addItem(f"{message.sender.address} — {message.subject}")
        if page.messages:
            self.messages.setCurrentRow(0)
        else:
            self.details.setPlainText(
                "No cached messages for this date. This is not a live Inbox check."
            )
        self.set_busy(False)

    def _show_message(self, index: int) -> None:
        self.source.setEnabled(False)
        if self._page is None or not 0 <= index < len(self._page.messages):
            self.details.clear()
            return
        message = self._page.messages[index]
        received = message.received_at_utc.astimezone(ZoneInfo(self._page.timezone_name))
        self.details.setPlainText(
            f"From: {message.sender.address}\nSubject: {message.subject}\n"
            f"Received: {received.isoformat(timespec='minutes')}\n"
            f"In Inbox at last observation: {'yes' if message.is_in_inbox else 'no'}\n\n"
            f"Saved preview (not a downloaded body):\n{message.body_preview}"
        )
        self.source.setEnabled(
            message.web_link.scheme == "https" and message.web_link.host == "mail.google.com"
        )

    def _open_source(self) -> None:
        index = self.messages.currentRow()
        if self._page is not None and 0 <= index < len(self._page.messages):
            link = self._page.messages[index].web_link
            if link.scheme == "https" and link.host == "mail.google.com":
                QDesktopServices.openUrl(QUrl(str(link)))
