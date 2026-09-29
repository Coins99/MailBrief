"""The Write with AI panel: parts, options, preview, first-use consent and cancel."""

from typing import Any

import pytest
from pytestqt.qtbot import QtBot

from mailbrief.domain.drafting import (
    DraftContextPart,
    DraftingOptions,
    DraftingPreview,
    PreviewLine,
)
from mailbrief.domain.drafts import DraftKind, DraftLength, DraftTone
from mailbrief.ui.drafting_panel import REPLY_EMAIL_LABEL, SOURCE_EMAIL_LABEL, DraftingPanel

ALL = frozenset(DraftContextPart)


@pytest.fixture
def panel(qtbot: QtBot) -> DraftingPanel:
    result = DraftingPanel()
    qtbot.addWidget(result)
    return result


def recorded(signal: Any) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []
    signal.connect(lambda *args: calls.append(args))
    return calls


def preview(first_use: bool) -> DraftingPreview:
    return DraftingPreview(
        provider="groq",
        model="m",
        privacy_notice="n",
        first_use=first_use,
        lines=(PreviewLine(label="Your current text: title and body", characters=5),),
    )


def test_only_available_parts_are_offered_and_ticked(panel: DraftingPanel) -> None:
    panel.offer(ALL, DraftKind.REPLY)
    assert panel.email_box.text() == REPLY_EMAIL_LABEL
    assert all(
        box.isChecked() and not box.isHidden()
        for box in (panel.email_box, panel.action_box, panel.text_box)
    )
    assert panel.no_parts.isHidden()

    panel.offer(frozenset({DraftContextPart.SOURCE_EMAIL}), DraftKind.NOTE)
    assert panel.email_box.text() == SOURCE_EMAIL_LABEL
    assert panel.action_box.isHidden() and panel.text_box.isHidden()

    panel.offer(frozenset(), DraftKind.MESSAGE)
    assert not panel.no_parts.isHidden()
    options = panel.options()
    assert options is not None and options.parts == frozenset()


def test_options_follow_the_form(panel: DraftingPanel) -> None:
    panel.offer(ALL, DraftKind.REPLY)
    panel.action_box.setChecked(False)
    panel.tone.setCurrentIndex(3)
    panel.length.setCurrentIndex(0)
    panel.instructions.setPlainText("  Accept politely.  ")

    assert panel.options() == DraftingOptions(
        parts=frozenset({DraftContextPart.SOURCE_EMAIL, DraftContextPart.CURRENT_TEXT}),
        tone=DraftTone.DIRECT,
        length=DraftLength.SHORT,
        instructions="Accept politely.",
    )


def test_instructions_are_limited(panel: DraftingPanel) -> None:
    requests = recorded(panel.prepare_requested)
    panel.instructions.setPlainText("x" * 1_001)

    assert panel.instructions_counter.text() == "1,001 / 1,000 — too long"
    assert not panel.continue_button.isEnabled()
    assert panel.options() is None
    panel._continue()
    assert requests == []
    panel.instructions.setPlainText("x" * 1_000)
    assert panel.continue_button.isEnabled()


def test_continue_locks_the_form_and_asks_for_a_preview(panel: DraftingPanel) -> None:
    requests = recorded(panel.prepare_requested)
    panel.offer(frozenset({DraftContextPart.CURRENT_TEXT}), DraftKind.NOTE)

    panel.continue_button.click()

    assert requests[0][0].parts == frozenset({DraftContextPart.CURRENT_TEXT})
    assert not panel.form.isEnabled() and not panel.continue_button.isEnabled()
    assert panel.working.text() == "Preparing what will be sent…"
    assert not panel.cancel_button.isHidden()


def test_first_use_needs_the_consent_box_before_sending(panel: DraftingPanel) -> None:
    sends = recorded(panel.send_requested)
    panel.continue_button.click()

    panel.show_preview(preview(first_use=True), ("Line one", "Line two"))

    assert panel.preview_text.toPlainText() == "Line one\nLine two"
    assert not panel.agree_box.isHidden()
    assert not panel.send_button.isEnabled()
    panel.agree_box.setChecked(True)
    assert panel.send_button.isEnabled()
    panel.send_button.click()
    assert sends == [(True,)]


def test_later_uses_can_send_at_once(panel: DraftingPanel) -> None:
    sends = recorded(panel.send_requested)

    panel.show_preview(preview(first_use=False), ("Line",))

    assert panel.agree_box.isHidden()
    panel.send_button.click()
    assert sends == [(False,)]


def test_generating_then_cancel_and_reset(panel: DraftingPanel) -> None:
    cancels = recorded(panel.cancel_requested)
    panel.show_preview(preview(first_use=False), ("Line",))

    panel.generating()
    assert panel.preview.isHidden()
    assert panel.working.text() == "Writing with Groq…"
    panel.cancel_button.click()
    assert cancels == [()]

    panel.reset()
    assert panel.cancel_button.isHidden() and panel.preview.isHidden()
    assert panel.form.isEnabled() and not panel.continue_button.isHidden()
