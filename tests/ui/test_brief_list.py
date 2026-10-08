"""The painted brief list: rows, chips, keyboard movement and accessible text."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QStyleOptionViewItem
from pytestqt.qtbot import QtBot

from mailbrief.domain.actions import ProposalState
from mailbrief.domain.analysis import DeadlinePrecision, FollowUpKind
from mailbrief.domain.digests import SECTION_TITLES, DigestItem
from mailbrief.domain.messages import EmailContact
from mailbrief.ui.brief_list import (
    KIND_ROLE,
    ROW_ROLE,
    BriefListView,
    BriefRow,
    Chip,
    ChipTone,
    build_rows,
    deadline_chip,
    proposal_chip,
    row_height,
    sender_text,
    sender_with_address,
)
from tests.factories import make_digest_item, make_proposal
from tests.ui.workspace_fixtures import ZONE, mockup_digest

TORONTO = ZoneInfo(ZONE)
TODAY = date(2026, 10, 6)  # A Tuesday: the mockup brief's day.


def test_rows_add_a_header_whenever_the_section_changes() -> None:
    brief = mockup_digest()
    rows = build_rows(brief.digest, brief.proposals, TODAY, TORONTO)
    assert [(row.kind, row.title) for row in rows] == [
        ("header", "Actions"),
        ("item", "Q3 budget: approval needed by Friday"),
        ("item", "Contract renewal options"),
        ("header", "Deadlines"),
        ("item", "Invoice 2041"),
        ("header", "Replies in threads you track"),
        ("item", "Re: Conference deck"),
        ("header", "Highlights"),
        ("item", "Office closed Monday"),
    ]
    assert rows[1].sender == "Priya Shah"
    assert rows[1].chips == (Chip("Due Fri 17:00", ChipTone.WARNING),)
    assert rows[4].chips == (Chip("Due Oct 9", ChipTone.WARNING),)
    assert rows[6].chips == (Chip("Proposes a new deadline", ChipTone.ACCENT),)
    assert rows[2].chips == ()


@pytest.mark.parametrize(
    ("name", "address", "expected"),
    [
        ("Priya Shah", "priya@example.com", "Priya Shah <priya@example.com>"),
        ("Priya Shah <priya@corp.example>", "attacker@evil.example", "attacker@evil.example"),
        ("priya@corp.example", "attacker@evil.example", "attacker@evil.example"),
        ("", "sam@example.com", "sam@example.com"),
        (None, "sam@example.com", "sam@example.com"),
        ("SAM@example.com", "sam@example.com", "sam@example.com"),  # The address itself.
        ("  Priya\n Shah ", "priya@example.com", "Priya Shah <priya@example.com>"),
    ],
)
def test_sender_with_address_never_hides_the_address(
    name: str | None, address: str, expected: str
) -> None:
    assert sender_with_address(EmailContact(name=name, address=address)) == expected


def test_sender_falls_back_to_the_address() -> None:
    assert sender_text(EmailContact(name=None, address="a@example.com")) == "a@example.com"
    assert sender_text(EmailContact(name="", address="a@example.com")) == "a@example.com"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {
                "deadline_text": "Friday 5pm",
                "deadline_precision": DeadlinePrecision.DATETIME,
                "deadline_date": date(2026, 10, 9),
                "deadline_at_utc": datetime(2026, 10, 9, 21, 0, tzinfo=UTC),
            },
            "Due Fri 17:00",
        ),
        (
            {
                "deadline_text": "October 9",
                "deadline_precision": DeadlinePrecision.DATE,
                "deadline_date": date(2026, 10, 9),
                "deadline_at_utc": None,
            },
            "Due Oct 9",
        ),
        (
            {
                "deadline_text": "as soon as you possibly can, please",
                "deadline_precision": DeadlinePrecision.UNRESOLVED,
                "deadline_at_utc": None,
            },
            "Due as soon as you possibly…",
        ),
        (
            {"deadline_text": "ASAP", "deadline_precision": DeadlinePrecision.UNRESOLVED},
            "Due ASAP",
        ),
        ({"deadline_at_utc": None}, None),
    ],
)
def test_deadline_chip_text(overrides: dict[str, object], expected: str | None) -> None:
    chip = deadline_chip(make_digest_item(**overrides), TORONTO, TODAY, TORONTO)
    if expected is None:
        assert chip is None
    else:
        assert chip == Chip(expected, ChipTone.WARNING)
        if overrides["deadline_precision"] is DeadlinePrecision.UNRESOLVED:
            assert len(expected) <= len("Due ") + 24


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 10, 6), "Due Tue 17:00"),  # Today.
        (date(2026, 10, 9), "Due Fri 17:00"),  # This week.
        (date(2026, 10, 12), "Due Mon 17:00"),  # Six days out: still its weekday.
        (date(2026, 10, 13), "Due Oct 13 17:00"),  # Seven days out: its date.
        (date(2026, 10, 23), "Due Oct 23 17:00"),
        (date(2026, 10, 2), "Due Oct 2 17:00"),  # Past: never "Fri", which reads as ahead.
    ],
)
def test_a_timed_deadline_names_its_weekday_only_within_the_week(day: date, expected: str) -> None:
    due = datetime(day.year, day.month, day.day, 17, tzinfo=TORONTO).astimezone(UTC)
    item = make_digest_item(
        deadline_text="by then",
        deadline_precision=DeadlinePrecision.DATETIME,
        deadline_date=day,
        deadline_at_utc=due,
    )
    assert deadline_chip(item, TORONTO, TODAY, TORONTO) == Chip(expected, ChipTone.WARNING)


TOKYO, NEW_YORK = ZoneInfo("Asia/Tokyo"), ZoneInfo("America/New_York")


@pytest.mark.parametrize(
    ("due", "expected"),
    [
        # Tue Oct 13 10:00 in Tokyo is still Mon Oct 12 in New York: the sixth day, so it
        # gets a weekday (in the brief's zone, as the detail pane shows it).
        (datetime(2026, 10, 13, 10, tzinfo=TOKYO), "Due Tue 10:00"),
        # Tue Oct 6 08:00 in Tokyo, the owner's today, was yesterday in New York: a date.
        (datetime(2026, 10, 6, 8, tzinfo=TOKYO), "Due Oct 6 08:00"),
    ],
)
def test_the_week_is_judged_in_the_owner_s_zone(due: datetime, expected: str) -> None:
    """A Tokyo brief viewed in New York on Tue Oct 6."""
    item = make_digest_item(
        deadline_text="by then",
        deadline_precision=DeadlinePrecision.DATETIME,
        deadline_date=due.date(),
        deadline_at_utc=due.astimezone(UTC),
    )
    assert deadline_chip(item, TOKYO, TODAY, NEW_YORK) == Chip(expected, ChipTone.WARNING)


def test_a_date_only_deadline_keeps_its_date_however_far() -> None:
    item = make_digest_item(
        deadline_text="October 23",
        deadline_precision=DeadlinePrecision.DATE,
        deadline_date=date(2026, 10, 23),
        deadline_at_utc=None,
    )
    assert deadline_chip(item, TORONTO, TODAY, TORONTO) == Chip("Due Oct 23", ChipTone.WARNING)


@pytest.mark.parametrize(
    ("kind", "text"),
    [
        (FollowUpKind.CANCELLED, "Proposes completing an action"),
        (FollowUpKind.DELIVERED, "Proposes completing an action"),
    ],
)
def test_proposal_chip_uses_the_first_pending_proposal(kind: FollowUpKind, text: str) -> None:
    applied = make_proposal(id=1, state=ProposalState.APPLIED)
    pending = make_proposal(id=2, kind=kind)
    assert proposal_chip([applied, pending]) == Chip(text, ChipTone.ACCENT)
    assert proposal_chip([applied]) is None
    assert proposal_chip([]) is None
    assert mockup_digest().proposals["marco"][0].kind is FollowUpKind.NEW_DEADLINE


@pytest.fixture
def view(qtbot: QtBot) -> BriefListView:
    view = BriefListView()
    qtbot.addWidget(view)
    brief = mockup_digest()
    view.show_rows(build_rows(brief.digest, brief.proposals, TODAY, TORONTO))
    view.resize(340, 600)
    view.show()
    return view


def test_header_rows_cannot_be_reached(view: BriefListView) -> None:
    model = view.model()
    header, item = model.index(0, 0), model.index(1, 0)
    assert header.data(KIND_ROLE) == "header"
    assert model.flags(header) == Qt.ItemFlag.NoItemFlags
    assert model.flags(item) == Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
    assert model.flags(QModelIndex()) == Qt.ItemFlag.NoItemFlags
    assert view.currentIndex().row() == 1  # The first item, not its header.


def test_down_skips_the_next_sections_header(view: BriefListView) -> None:
    view.setFocus()
    view.setCurrentIndex(view.model().index(2, 0))  # The last Actions item.
    QTest.keyClick(view, Qt.Key.Key_Down)
    assert view.currentIndex().row() == 4
    assert view.currentIndex().data(Qt.ItemDataRole.DisplayRole) == "Invoice 2041"


def test_accessible_text_names_subject_sender_and_chips(view: BriefListView) -> None:
    model = view.model()
    assert model.index(0, 0).data(Qt.ItemDataRole.AccessibleTextRole) == "Actions"
    assert model.index(1, 0).data(Qt.ItemDataRole.AccessibleTextRole) == (
        "Q3 budget: approval needed by Friday, from Priya Shah, Due Fri 17:00"
    )
    assert model.index(2, 0).data(Qt.ItemDataRole.AccessibleTextRole) == (
        "Contract renewal options, from Lena Park"
    )
    assert model.index(99, 0).data(Qt.ItemDataRole.DisplayRole) is None
    assert model.index(1, 0).data(Qt.ItemDataRole.ToolTipRole) is None


def test_chips_make_a_row_taller(view: BriefListView) -> None:
    delegate = view.itemDelegate()
    option = QStyleOptionViewItem()
    with_chip = delegate.sizeHint(option, view.model().index(1, 0))
    without = delegate.sizeHint(option, view.model().index(2, 0))
    header = delegate.sizeHint(option, view.model().index(0, 0))
    assert with_chip.height() > without.height() > header.height() > 0
    assert with_chip.height() == row_height(True)
    assert without.height() == row_height(False)


def test_selection_reports_the_email(view: BriefListView) -> None:
    spy = QSignalSpy(view.item_selected)
    view.setCurrentIndex(view.model().index(6, 0))
    assert spy.count() == 1
    selected = spy.at(0)[0]
    assert isinstance(selected, DigestItem) and selected.message_key == "marco"


def test_return_activates_once(view: BriefListView) -> None:
    spy = QSignalSpy(view.activated)
    view.setFocus()
    QTest.keyClick(view, Qt.Key.Key_Return)
    QTest.keyClick(view, Qt.Key.Key_Enter)
    assert spy.count() == 2


def test_rows_paint_without_error(view: BriefListView) -> None:
    row = view.model().index(1, 0).data(ROW_ROLE)
    assert isinstance(row, BriefRow)
    assert not view.grab().isNull()


def test_empty_subject_reads_no_subject() -> None:
    brief = mockup_digest()
    blank = brief.digest.model_copy(
        update={"items": (brief.digest.items[0].model_copy(update={"subject": ""}),)}
    )
    rows = build_rows(blank, {}, TODAY, TORONTO)
    assert rows[1].title == "(no subject)"


def test_every_section_uses_the_shared_titles() -> None:
    sections = tuple(SECTION_TITLES)
    digest = mockup_digest().digest.model_copy(
        update={
            "items": tuple(
                make_digest_item(message_key=f"m{index}", position=index, section=section)
                for index, section in enumerate(sections)
            )
        }
    )
    headers = [row.title for row in build_rows(digest, {}, TODAY, TORONTO) if row.kind == "header"]
    assert headers == [SECTION_TITLES[section] for section in sections]
    assert "Decisions" in headers and "Replies in threads you track" in headers
