"""The Preferences tab: the owner's time zone, messages per brief, sender exclusions,
drafting defaults and AI limits (ADR 0014).

The panel only validates and emits; the window saves through the backend. Sender rules
are the owner's own text, shown back only here, in plain-text widgets.
"""

import functools
import zoneinfo

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from mailbrief.config import Settings
from mailbrief.domain.analysis import MAX_ANALYSIS_BATCH
from mailbrief.domain.preferences import (
    AI_BODY_CHARS_MAX,
    AI_OUTPUT_TOKENS_MAX,
    AI_OUTPUT_TOKENS_MIN,
    AI_REQUESTS_MAX,
    AI_TIMEOUT_MAX,
    AI_TIMEOUT_MIN,
    EXCLUSIONS_MAX,
    SHORTLIST_LIMIT_MAX,
    OwnerPreferences,
    PreferencesEdit,
    is_region_zone,
    normalize_exclusion,
)
from mailbrief.ui.drafting_panel import LENGTH_CHOICES, TONE_CHOICES

# Each AI limit's field, label and range; the spin box's minimum is one below the range
# and means "use the default".
_AI_FIELDS = (
    ("ai_body_character_limit", "Body characters sent", 1, AI_BODY_CHARS_MAX, ""),
    ("ai_max_output_tokens", "Output tokens", AI_OUTPUT_TOKENS_MIN, AI_OUTPUT_TOKENS_MAX, ""),
    ("ai_max_requests_per_run", "Requests per run", 1, AI_REQUESTS_MAX, ""),
    ("ai_batch_size", "Messages per AI request", 1, MAX_ANALYSIS_BATCH, ""),
    ("ai_timeout_seconds", "Timeout", AI_TIMEOUT_MIN, AI_TIMEOUT_MAX, " s"),
)
_ENVIRONMENT_NOTE = "When set, a MAILBRIEF_AI_* environment variable takes precedence."
_UNAVAILABLE = (
    "Saved preferences could not be read, so briefs and AI drafting are paused. "
    "Reset to defaults to continue."
)
_BAD_ZONE = "Choose a time zone from the list, such as America/Toronto."


@functools.cache
def region_zones() -> tuple[str, ...]:
    """Every UTC or IANA region zone this installation knows, sorted.

    Loading every zone takes a noticeable moment, so call this in a worker thread.
    """
    return tuple(sorted(name for name in zoneinfo.available_timezones() if is_region_zone(name)))


def _plain(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _default_text(name: str) -> str:
    default = Settings.model_fields[name].default
    return f"Default ({default:g})" if isinstance(default, float) else f"Default ({default})"


class PreferencesPanel(QWidget):
    """``save_requested(edit, revision)`` carries a valid PreferencesEdit and the revision
    it was loaded at; ``reset_requested()`` asks for the defaults. Problems with the form
    go to ``status_changed(text)``."""

    save_requested = Signal(object, int)
    reset_requested = Signal()
    status_changed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._revision = 0
        self._zones: tuple[str, tuple[str, ...]] | None = None
        layout = QVBoxLayout(self)
        self.form_fields = QWidget()
        form = QFormLayout(self.form_fields)
        self.time_zone = QComboBox()
        self.time_zone.setEditable(True)
        self.time_zone.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.time_zone.setAccessibleName("Time zone for your daily brief")
        form.addRow("&Time zone", self.time_zone)
        self.shortlist_limit = QSpinBox()
        self.shortlist_limit.setRange(1, SHORTLIST_LIMIT_MAX)
        self.shortlist_limit.setAccessibleName("Messages per brief")
        form.addRow("&Messages per brief", self.shortlist_limit)
        self.senders = QPlainTextEdit()
        self.senders.setAccessibleName("Excluded senders, one address or @domain per line")
        self.senders.setPlaceholderText("boss@example.com\n@newsletters.example.com")
        self.senders.setTabChangesFocus(True)
        self.senders.setMaximumHeight(110)
        form.addRow("&Excluded senders", self.senders)
        form.addRow(
            "",
            _plain(
                "One address or @domain per line; @domain also covers its subdomains. Mail "
                "from these senders is never analyzed or sent to AI drafting."
            ),
        )
        self.tone = QComboBox()
        for _tone, name in TONE_CHOICES:
            self.tone.addItem(name)
        self.tone.setAccessibleName("Default drafting tone")
        form.addRow("Drafting t&one", self.tone)
        self.length = QComboBox()
        for _length, name in LENGTH_CHOICES:
            self.length.addItem(name)
        self.length.setAccessibleName("Default drafting length")
        form.addRow("Drafting len&gth", self.length)
        self.ai_limits: dict[str, QSpinBox] = {}
        for name, label, low, high, suffix in _AI_FIELDS:
            box = QSpinBox()
            box.setRange(low - 1, high)
            box.setSuffix(suffix)
            box.setSpecialValueText(_default_text(name))
            box.setAccessibleName(f"AI limit: {label.lower()}")
            self.ai_limits[name] = box
            form.addRow(label, box)
        form.addRow("", _plain(_ENVIRONMENT_NOTE))
        layout.addWidget(self.form_fields)
        buttons = QHBoxLayout()
        self.save_button = QPushButton("Save &preferences")
        self.reset_button = QPushButton("Reset to defaults")
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.reset_button)
        layout.addLayout(buttons)
        self.save_button.clicked.connect(self._save)
        self.reset_button.clicked.connect(self.reset_requested.emit)
        self.set_zones("unknown", ())

    def set_zones(self, system_zone: str, zones: tuple[str, ...]) -> None:
        """The choices: the system time zone first, then ``zones`` (see region_zones())."""
        if self._zones == (system_zone, zones):
            return
        self._zones = (system_zone, zones)
        self.time_zone.clear()
        self.time_zone.addItem(f"System time zone ({system_zone})", None)
        for name in zones:
            self.time_zone.addItem(name, name)

    def set_preferences(self, preferences: OwnerPreferences) -> None:
        """Show saved preferences; a save later sends their revision."""
        self._revision = preferences.revision
        zone = preferences.time_zone
        index = 0 if zone is None else self.time_zone.findData(zone)
        if index < 0 and zone is not None:
            # Saved elsewhere with newer time zone data; it loads here, so offer it.
            self.time_zone.addItem(zone, zone)
            index = self.time_zone.count() - 1
        self.time_zone.setCurrentIndex(index)
        self.shortlist_limit.setValue(preferences.shortlist_limit)
        self.senders.setPlainText("\n".join(preferences.excluded_senders))
        self.tone.setCurrentIndex([tone for tone, _ in TONE_CHOICES].index(preferences.draft_tone))
        self.length.setCurrentIndex(
            [length for length, _ in LENGTH_CHOICES].index(preferences.draft_length)
        )
        for name, box in self.ai_limits.items():
            value = getattr(preferences, name)
            box.setValue(box.minimum() if value is None else round(value))
        self.save_button.setEnabled(True)

    def set_unavailable(self) -> None:
        """Show the defaults; only a reset can repair unreadable preferences."""
        self.set_preferences(OwnerPreferences.defaults())
        self.save_button.setEnabled(False)
        self.status_changed.emit(_UNAVAILABLE)

    def edit(self) -> PreferencesEdit | None:
        """The form as a valid edit, or None after reporting what is wrong."""
        zone = self._chosen_zone()
        if zone == "":
            self.status_changed.emit(_BAD_ZONE)
            return None
        rules: list[str] = []
        for number, line in enumerate(self.senders.toPlainText().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                rules.append(normalize_exclusion(line))
            except ValueError:
                self.status_changed.emit(f"Line {number} is not an address or @domain.")
                return None
        if len(dict.fromkeys(rules)) > EXCLUSIONS_MAX:
            self.status_changed.emit(f"At most {EXCLUSIONS_MAX} excluded senders.")
            return None
        values: dict[str, object] = {
            name: None if box.value() == box.minimum() else box.value()
            for name, box in self.ai_limits.items()
        }
        values.update(
            time_zone=zone,
            shortlist_limit=self.shortlist_limit.value(),
            excluded_senders=tuple(rules),
            draft_tone=TONE_CHOICES[self.tone.currentIndex()][0],
            draft_length=LENGTH_CHOICES[self.length.currentIndex()][0],
        )
        return PreferencesEdit.model_validate(values)

    def _chosen_zone(self) -> str | None:
        """None for the system zone, a listed zone in its own spelling, or "" when the
        typed text matches no entry."""
        index = self.time_zone.findText(
            self.time_zone.currentText().strip(), Qt.MatchFlag.MatchFixedString
        )
        if index < 0:
            return ""
        data = self.time_zone.itemData(index)
        return None if data is None else str(data)

    def _save(self) -> None:
        edit = self.edit()
        if edit is not None:
            self.save_requested.emit(edit, self._revision)
