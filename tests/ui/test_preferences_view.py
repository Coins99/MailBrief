"""The Preferences tab and the window's use of the owner's preferences."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from pytestqt.qtbot import QtBot

from mailbrief.domain.cached_mail import CachedAccount, CachedMailPage
from mailbrief.domain.drafting import DraftContextPart
from mailbrief.domain.drafts import DraftKind, DraftLength, DraftTone
from mailbrief.domain.preferences import OwnerPreferences, PreferencesEdit
from mailbrief.services.preferences import PreferencesUnavailableError
from mailbrief.ui.drafting_panel import DraftingPanel
from mailbrief.ui.main_window import MainWindow
from mailbrief.ui.preferences_view import PreferencesPanel, region_zones
from tests.ui.brief_view import shown_text
from tests.ui.test_workflow import FakeBackend, finish

ZONES = ("America/New_York", "America/Toronto", "Asia/Tokyo", "UTC")
SAVED = OwnerPreferences(
    revision=4,
    time_zone="America/Toronto",
    shortlist_limit=3,
    excluded_senders=("@news.example.com", "boss@example.com"),
    draft_tone=DraftTone.FORMAL,
    draft_length=DraftLength.LONG,
    ai_body_character_limit=2_000,
    ai_timeout_seconds=90,
)


@pytest.fixture
def panel(qtbot: QtBot) -> PreferencesPanel:
    result = PreferencesPanel()
    qtbot.addWidget(result)
    result.set_zones("Asia/Tokyo", ZONES)
    return result


def emitted(panel: PreferencesPanel) -> tuple[list[tuple[object, int]], list[str]]:
    saves: list[tuple[object, int]] = []
    problems: list[str] = []
    panel.save_requested.connect(lambda edit, revision: saves.append((edit, revision)))
    panel.status_changed.connect(problems.append)
    return saves, problems


def test_saved_preferences_round_trip_with_their_revision(panel: PreferencesPanel) -> None:
    saves, problems = emitted(panel)
    panel.set_preferences(SAVED)

    assert panel.time_zone.currentText() == "America/Toronto"
    assert panel.senders.toPlainText() == "@news.example.com\nboss@example.com"
    assert panel.ai_limits["ai_batch_size"].text() == "Default (1)"
    assert panel.ai_limits["ai_timeout_seconds"].text() == "90 s"
    panel.save_button.click()

    assert problems == []
    ((edit, revision),) = saves
    assert revision == 4
    assert edit == PreferencesEdit(**SAVED.model_dump(exclude={"revision", "updated_at_utc"}))


def test_sentinels_mean_the_defaults(panel: PreferencesPanel) -> None:
    saves, _ = emitted(panel)
    panel.set_preferences(SAVED)
    for box in panel.ai_limits.values():
        box.setValue(box.minimum())
    panel.ai_limits["ai_max_output_tokens"].setValue(1_024)
    panel.time_zone.setCurrentIndex(0)

    panel.save_button.click()

    ((edit, _),) = saves
    assert isinstance(edit, PreferencesEdit)
    assert edit.time_zone is None
    assert edit.ai_max_output_tokens == 1_024
    assert edit.ai_body_character_limit is None and edit.ai_timeout_seconds is None
    assert panel.time_zone.itemText(0) == "System time zone (Asia/Tokyo)"


def test_every_field_has_an_accessible_name(panel: PreferencesPanel) -> None:
    fields = [panel.time_zone, panel.shortlist_limit, panel.senders, panel.tone, panel.length]
    fields.extend(panel.ai_limits.values())
    assert all(field.accessibleName() for field in fields)


def test_an_invalid_rule_line_emits_nothing(panel: PreferencesPanel) -> None:
    saves, problems = emitted(panel)
    panel.set_preferences(OwnerPreferences.defaults())
    panel.senders.setPlainText("boss@example.com\n\nnot a sender\n")

    panel.save_button.click()

    assert saves == []
    assert problems == ["Line 3 is not an address or @domain."]


def test_a_typed_zone_takes_the_listed_spelling_or_is_refused(panel: PreferencesPanel) -> None:
    saves, problems = emitted(panel)
    panel.set_preferences(OwnerPreferences.defaults())

    panel.time_zone.setEditText("asia/tokyo")
    panel.save_button.click()
    panel.time_zone.setEditText("EST")
    panel.save_button.click()

    ((edit, _),) = saves
    assert isinstance(edit, PreferencesEdit) and edit.time_zone == "Asia/Tokyo"
    assert problems == ["Choose a time zone from the list, such as America/Toronto."]


def test_a_saved_zone_missing_from_the_list_is_still_offered(panel: PreferencesPanel) -> None:
    saves, _ = emitted(panel)
    panel.set_preferences(OwnerPreferences(revision=1, time_zone="Europe/Paris"))
    panel.save_button.click()
    ((edit, _),) = saves
    assert isinstance(edit, PreferencesEdit) and edit.time_zone == "Europe/Paris"


def test_unreadable_preferences_offer_only_a_reset(panel: PreferencesPanel) -> None:
    _, problems = emitted(panel)
    resets: list[bool] = []
    panel.reset_requested.connect(lambda: resets.append(True))

    panel.set_unavailable()

    assert not panel.save_button.isEnabled()
    assert "Reset to defaults" in problems[0]
    panel.reset_button.click()
    assert resets == [True]
    panel.set_preferences(SAVED)
    assert panel.save_button.isEnabled()


def test_region_zones_lists_regions_only() -> None:
    zones = region_zones()
    assert "America/New_York" in zones and "UTC" in zones
    assert not any(zone == "EST" or zone.startswith("Etc/") for zone in zones)
    assert list(zones) == sorted(zones)


def test_drafting_defaults_apply_when_offered_but_not_after_a_cancel(qtbot: QtBot) -> None:
    drafting = DraftingPanel()
    qtbot.addWidget(drafting)
    drafting.set_defaults(DraftTone.WARM, DraftLength.SHORT)

    drafting.offer(frozenset({DraftContextPart.CURRENT_TEXT}), DraftKind.NOTE)
    options = drafting.options()
    assert options is not None
    assert (options.tone, options.length) == (DraftTone.WARM, DraftLength.SHORT)

    drafting.tone.setCurrentIndex(2)  # The owner's own choice for this draft.
    drafting.reset()  # As after a cancel or a failure.
    options = drafting.options()
    assert options is not None and options.tone is DraftTone.FORMAL


# The window.


@pytest.fixture
def backend() -> FakeBackend:
    result = FakeBackend()
    result.owner_preferences = SAVED
    return result


@pytest.fixture
def window(qtbot: QtBot, backend: FakeBackend) -> MainWindow:
    result = MainWindow(backend)
    qtbot.addWidget(result)
    return result


async def test_startup_applies_the_owner_s_zone_and_drafting_defaults(
    window: MainWindow,
) -> None:
    await window.initialize()

    assert window.zone == ZoneInfo("America/Toronto")
    assert window.cached_dialog.zone == window.zone
    # Generated at 12:00 UTC in a UTC brief: the full sentence gives that time and names the
    # zone, as the short line does, because Toronto would show another time.
    assert "Saved Fri Sep 4, 12:00 (UTC)." in shown_text(window)
    window.draft_editor.ai_panel.offer(frozenset(), DraftKind.NOTE)
    options = window.draft_editor.ai_panel.options()
    assert options is not None
    assert (options.tone, options.length) == (DraftTone.FORMAL, DraftLength.LONG)


async def test_unreadable_preferences_fall_back_to_the_system_zone_for_display(
    window: MainWindow, backend: FakeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tzlocal.get_localzone", lambda: ZoneInfo("Asia/Tokyo"))
    backend.owner_fail = PreferencesUnavailableError()

    await window.initialize()

    assert window.zone == ZoneInfo("Asia/Tokyo")
    assert window.status.text() == str(PreferencesUnavailableError())
    assert "private subject" in shown_text(window)  # The saved brief still shows.


async def test_settings_show_the_owner_s_preferences(
    window: MainWindow, backend: FakeBackend
) -> None:
    window.start(window._open_settings)
    await finish(window)

    panel = window.settings_dialog.preferences_panel
    assert window.settings_dialog.tabs.count() == 2
    assert panel.time_zone.currentText() == "America/Toronto"
    assert panel.shortlist_limit.value() == 3
    assert panel.time_zone.itemText(0).startswith("System time zone (")
    window.settings_dialog.reject()


async def test_saving_applies_the_new_zone_and_refreshes_the_views(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    window.start(window._open_settings)
    await finish(window)
    panel = window.settings_dialog.preferences_panel
    loads, lists = backend.loads, backend.list_calls
    panel.time_zone.setCurrentIndex(panel.time_zone.findData("Asia/Tokyo"))

    panel.save_button.click()
    await finish(window)

    ((edit, revision),) = backend.owner_saves
    assert revision == 4 and edit.time_zone == "Asia/Tokyo"
    assert window.zone == ZoneInfo("Asia/Tokyo")
    assert window.status.text() == "Preferences saved."
    assert window.settings_dialog.status.text() == "Preferences saved."
    assert backend.loads > loads and backend.list_calls > lists
    # The save time stays in the brief's zone; Tokyo would show another time, so it's named.
    assert "Saved Fri Sep 4, 12:00 (UTC)." in shown_text(window)

    panel.save_button.click()  # The panel now holds the new revision.
    await finish(window)
    assert backend.owner_saves[-1][1] == 5
    window.settings_dialog.reject()


async def test_a_stale_save_says_so_in_the_dialog(window: MainWindow, backend: FakeBackend) -> None:
    await window.initialize()
    window.start(window._open_settings)
    await finish(window)
    backend.owner_preferences = SAVED.model_copy(update={"revision": 5})  # Changed elsewhere.

    window.settings_dialog.preferences_panel.save_button.click()
    await finish(window)

    message = "Preferences changed since they were loaded; reopen Settings."
    assert window.settings_dialog.status.text() == message
    assert window.zone == ZoneInfo("America/Toronto")
    window.settings_dialog.reject()


async def test_unreadable_preferences_can_be_reset_from_settings(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.owner_fail = PreferencesUnavailableError()
    window.start(window._open_settings)
    await finish(window)
    panel = window.settings_dialog.preferences_panel
    assert not panel.save_button.isEnabled()
    assert window.settings_dialog.status.text() == str(PreferencesUnavailableError())

    panel.reset_button.click()
    await finish(window)

    assert backend.owner_preferences.time_zone is None
    assert panel.save_button.isEnabled()
    assert window.settings_dialog.status.text() == "Preferences reset to the defaults."
    window.settings_dialog.reject()


async def test_offline_browsing_shows_the_last_sync_in_the_owner_s_zone(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    page = CachedMailPage(
        account=CachedAccount(
            account_id=1,
            email_address="owner@example.com",
            last_sync_at_utc=datetime(2026, 9, 4, 12, tzinfo=UTC),
        ),
        local_date=date(2026, 9, 4),
        timezone_name="America/Toronto",
    )
    window.cached_dialog.today = lambda: date(2026, 9, 4)
    window.cached_dialog.show_page(page)
    text = window.cached_dialog.status.text()
    assert "Fri Sep 4 (America/Toronto)" in text
    assert "last complete sync: Fri Sep 4, 08:00." in text


async def test_a_brief_refused_for_unreadable_preferences_says_why(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    backend.fail = PreferencesUnavailableError()

    window.start(window._generate)
    await finish(window)

    assert window.status.text() == str(PreferencesUnavailableError())
    assert backend.selected is None  # Nothing reached review, let alone Groq.
