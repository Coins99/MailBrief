"""The selected email's summary, deadline, suggestions and proposals.

Buttons only emit requests, with the IDs and revisions shown; the window decides what
happens. Mail and AI text appears only in plain-text labels and escaped button text, never
in a tooltip, and the pane never opens a URL itself.
"""

from collections.abc import Callable, Sequence
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from mailbrief.domain.actions import (
    TARGET_REASON_TEXT,
    ActionProposal,
    ProposalState,
    SuggestionState,
    SuggestionView,
    ThreadLink,
)
from mailbrief.domain.analysis import ActionOwnership, DeadlinePrecision
from mailbrief.domain.digests import DigestItem
from mailbrief.ui.brief_list import NO_SUBJECT, sender_text
from mailbrief.ui.deadline_text import deadline_text
from mailbrief.ui.hairline import HairlineFrame
from mailbrief.ui.labels import button_label, plain_label, short_button_label, wrap_label
from mailbrief.ui.proposals_view import effect_text
from mailbrief.ui.theme import SMALL_PX, TEXT_PX, TITLE_PX, current_tokens, icon, icon_pixmap

# What a suggestion or proposal request asks for.
ACCEPT = "accept"
DISMISS = "dismiss"
APPLY = "apply"
EMPTY_TEXT = "Select an email to see its summary and suggestions."
DETAIL_MAX_WIDTH = 720
_ICON_PX = 14


def item_deadline_text(item: DigestItem, zone: ZoneInfo) -> str | None:
    """The email's own deadline in the brief's zone, or None."""
    if item.deadline_precision is DeadlinePrecision.DATETIME and item.deadline_at_utc:
        local = item.deadline_at_utc.astimezone(zone)
        return f"Due {local:%a %b} {local.day}, {local:%H:%M}"
    if item.deadline_precision is DeadlinePrecision.DATE and item.deadline_date:
        day = item.deadline_date
        return f"Due {day:%a %b} {day.day}"
    if item.deadline_precision is DeadlinePrecision.UNRESOLVED and item.deadline_text:
        return f"Due “{item.deadline_text}”"
    return None


def suggestion_meta(view: SuggestionView, zone: ZoneInfo) -> str:
    """Whose it is, then its target, else its deadline, else that none was stated."""
    suggestion = view.suggestion
    text = "Yours." if suggestion.ownership is ActionOwnership.MINE else "Waiting for someone."
    target, reason = suggestion.suggested_target_date, suggestion.target_reason
    due = deadline_text(suggestion, zone)
    if target is not None and reason is not None:
        return f"{text} Target {target:%a %b} {target.day}, {TARGET_REASON_TEXT[reason]}."
    if due is not None:
        return f"{text} Due {due}."
    return f"{text} No deadline stated."


def is_gmail_link(url: str) -> bool:
    """Only https links on mail.google.com are ever offered for opening."""
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname == "mail.google.com"


def outline_button(
    text: str, icon_name: str | None = None, *, shorten: bool = False
) -> QPushButton:
    """An outline button. With ``shorten``, long mail-derived text is cut to one short
    line and the full text becomes the accessible name, never a tooltip."""
    button = QPushButton(short_button_label(text) if shorten else button_label(text))
    if shorten:
        button.setAccessibleName(text)
    button.setProperty("variant", "outline")
    button.setAutoDefault(False)
    if icon_name is not None:
        button.setIcon(icon(icon_name, current_tokens().text, _ICON_PX))
        button.setIconSize(QSize(_ICON_PX, _ICON_PX))
    return button


class BriefDetailPane(QScrollArea):
    """``suggestion_requested(kind, suggestion_id)`` with ACCEPT or DISMISS,
    ``accept_into_requested(suggestion_id, public_id, revision)``,
    ``proposal_requested(kind, proposal_id, action_revision)`` with APPLY or DISMISS,
    ``reply_requested(account_email, message_key)`` and ``source_requested(url)``."""

    suggestion_requested = Signal(str, int)
    accept_into_requested = Signal(int, str, int)
    proposal_requested = Signal(str, int, int)
    reply_requested = Signal(str, str)
    source_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("briefDetail")
        self.setAccessibleName("Selected email")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.title: QLabel | None = None
        self.show_empty()

    def _fresh(self) -> QVBoxLayout:
        content = QWidget()
        content.setObjectName("briefDetailContent")
        # A readable measure on wide windows; the scroll area keeps it at the left.
        content.setMaximumWidth(DETAIL_MAX_WIDTH)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        self.setWidget(content)  # Deletes the previous content.
        return layout

    def show_empty(self, text: str = EMPTY_TEXT) -> None:
        layout = self._fresh()
        self.title = None
        layout.addWidget(plain_label(text, tone="muted", px=TEXT_PX))
        layout.addStretch(1)

    def show_item(
        self,
        item: DigestItem,
        *,
        account_email: str,
        timezone_name: str,
        links: Sequence[ThreadLink] = (),
        proposals: Sequence[ActionProposal] = (),
    ) -> None:
        zone = ZoneInfo(timezone_name)
        layout = self._fresh()
        self.title = wrap_label(item.subject or NO_SUBJECT, px=TITLE_PX, medium=True)
        layout.addWidget(self.title)
        layout.addWidget(wrap_label(sender_text(item.sender), tone="secondary", px=TEXT_PX))
        layout.addWidget(wrap_label(item.summary, px=TEXT_PX))
        if item.action_text and item.action_text != item.summary:
            layout.addWidget(wrap_label(item.action_text, tone="secondary", px=TEXT_PX))
        due = item_deadline_text(item, zone)
        if due is not None:
            layout.addLayout(self._deadline_row(due))
        for link in links:
            if not link.is_source:
                layout.addWidget(
                    wrap_label(f"Continues: “{link.title}”", tone="secondary", px=TEXT_PX)
                )
        for view in item.suggestions:
            if view.state is SuggestionState.PENDING:
                layout.addWidget(self._suggestion_card(view, links, zone))
            elif view.state is SuggestionState.ACCEPTED:
                layout.addWidget(
                    wrap_label(f"Accepted: {view.suggestion.title}", tone="secondary", px=TEXT_PX)
                )
        for proposal in proposals:
            if proposal.state is ProposalState.PENDING:
                layout.addWidget(self._proposal_card(proposal, zone))
        layout.addLayout(self._footer(item, account_email))
        layout.addStretch(1)

    def _deadline_row(self, text: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        clock = QLabel()
        clock.setPixmap(icon_pixmap("clock", current_tokens().warning_fg, _ICON_PX, 2.0))
        clock.setAccessibleName("Deadline")
        row.addWidget(clock)
        row.addWidget(wrap_label(text, tone="warning", px=TEXT_PX), 1)
        return row

    def _suggestion_card(
        self, view: SuggestionView, links: Sequence[ThreadLink], zone: ZoneInfo
    ) -> HairlineFrame:
        card = HairlineFrame()
        card.setObjectName("suggestionCard")
        card.setAccessibleName("Suggested action")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(wrap_label(view.suggestion.title, px=TEXT_PX, medium=True))
        layout.addWidget(wrap_label(suggestion_meta(view, zone), tone="secondary", px=SMALL_PX))
        for step in view.suggestion.steps:  # The suggested plan, in order.
            layout.addWidget(wrap_label(f"· {step}", tone="secondary", px=SMALL_PX))
        layout.addSpacing(4)
        decisions = QHBoxLayout()
        decisions.setSpacing(8)
        accept = outline_button("Accept")
        accept.clicked.connect(
            lambda _checked=False, sid=view.suggestion_id: self.suggestion_requested.emit(
                ACCEPT, sid
            )
        )
        dismiss = outline_button("Dismiss")
        dismiss.clicked.connect(
            lambda _checked=False, sid=view.suggestion_id: self.suggestion_requested.emit(
                DISMISS, sid
            )
        )
        decisions.addWidget(accept)
        decisions.addWidget(dismiss)
        decisions.addStretch(1)
        layout.addLayout(decisions)
        for link in links:
            # One row each, so a long title never widens the pane.
            row = QHBoxLayout()
            button = outline_button(f"Add to “{link.title}”", shorten=True)
            button.clicked.connect(self._into(view.suggestion_id, link))
            row.addWidget(button)
            row.addStretch(1)
            layout.addSpacing(4)
            layout.addLayout(row)
        return card

    def _into(self, suggestion_id: int, link: ThreadLink) -> Callable[[], None]:
        """An "Add to" handler bound to this suggestion and action, not the loop's last."""
        return lambda: self.accept_into_requested.emit(suggestion_id, link.public_id, link.revision)

    def _proposal_card(self, proposal: ActionProposal, zone: ZoneInfo) -> HairlineFrame:
        card = HairlineFrame()
        card.setObjectName("proposalCard")
        card.setAccessibleName("Proposed update")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(
            wrap_label(f"Proposes for “{proposal.action_title}”: “{proposal.evidence}”", px=TEXT_PX)
        )
        row = QHBoxLayout()
        row.setSpacing(8)
        apply = outline_button(effect_text(proposal, zone), shorten=True)
        apply.clicked.connect(
            lambda _checked=False, pid=proposal.id, revision=proposal.action_revision: (
                self.proposal_requested.emit(APPLY, pid, revision)
            )
        )
        dismiss = outline_button("Dismiss")
        dismiss.clicked.connect(
            lambda _checked=False, pid=proposal.id, revision=proposal.action_revision: (
                self.proposal_requested.emit(DISMISS, pid, revision)
            )
        )
        row.addWidget(apply)
        row.addWidget(dismiss)
        row.addStretch(1)
        layout.addLayout(row)
        return card

    def _footer(self, item: DigestItem, account_email: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        reply = outline_button("Draft a reply", "pencil")
        reply.clicked.connect(
            lambda _checked=False, key=item.message_key: self.reply_requested.emit(
                account_email, key
            )
        )
        row.addWidget(reply)
        source = str(item.source_url)
        if is_gmail_link(source):
            open_gmail = outline_button("Open in Gmail", "external-link")
            open_gmail.clicked.connect(
                lambda _checked=False, url=source: self.source_requested.emit(url)
            )
            row.addWidget(open_gmail)
        row.addStretch(1)
        return row
