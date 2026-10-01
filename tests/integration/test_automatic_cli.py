"""Automatic runs from the CLI (ADR 0017): brief --automatic, ai-consent auto-send, the
permission in status and preferences show, and declines from --exclude and --include.

Gmail and Groq run over respx through the brief tests' synthetic mailbox, whose first message
is a1; nothing here may prompt unless a test has a typed answer ready for it.
"""

from pathlib import Path

import pytest
import respx

from mailbrief.diagnostics import gmail
from mailbrief.domain.preferences import PreferencesEdit
from tests.integration.test_brief_cli import (  # noqa: F401 - fixtures used by name
    Mailbox,
    groq_answers,
    mailbox,
    midday,
    replies,
    run_brief,
    vault,
)
from tests.integration.test_preferences_cli import corrupt, save_preferences
from tests.unit.providers.groq.groq_fixtures import sent_messages

pytestmark = pytest.mark.respx(assert_all_called=False)

NO_CONSENT = "Analyze once with Sync and review to give consent first."
READY = "new messages are ready to review; automatic analysis is off."


def automatic(path: Path, *options: str) -> int:
    return run_brief(path, "--automatic", *options)


def consent(path: Path, *arguments: str) -> int:
    return gmail.main(["ai-consent", *arguments, "--database", str(path)])


def sync(path: Path, *options: str) -> int:
    arguments = ["sync", "--silent-only", "--database", str(path), "--timezone", "UTC"]
    return gmail.main([*arguments, *options])


def full_body_requests(router: respx.MockRouter) -> int:
    return sum(call.request.url.params.get("format") == "full" for call in router.calls)


def consented(
    path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One ordinary brief, which records consent; the next prompt is the test's own."""
    replies(monkeypatch, "yes")
    assert run_brief(path) == 0
    capsys.readouterr()


# Without permission


def test_without_permission_an_automatic_run_only_counts_what_is_ready(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mailbox.add("a2")
    mailbox.add("a3")
    route = groq_answers(respx_mock)
    replies(monkeypatch)  # Any prompt fails the test.
    path = tmp_path / "auto.sqlite3"

    assert automatic(path) == 0

    output = capsys.readouterr().out
    assert "Inbox date: 2026-09-16 (UTC)" in output
    assert f"3 {READY}" in output
    assert route.call_count == 0 and full_body_requests(respx_mock) == 0
    for private in ("Approval needed", "sender@example.com"):
        assert private not in output
    # It synced, and saved no brief; consent was never asked for or recorded.
    assert gmail.main(["briefs", "list", "--database", str(path)]) == 0
    assert "No saved briefs." in capsys.readouterr().out
    assert consent(path, "status") == 0
    assert "Groq consent not granted" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("extra", "sentence"),
    [
        (0, "Nothing new to review."),
        (1, "1 new message is ready to review; automatic analysis is off."),
    ],
)
def test_the_ready_sentence_handles_none_and_one(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    extra: int,
    sentence: str,
) -> None:
    if extra == 0:
        mailbox.empty()
    groq_answers(respx_mock)
    replies(monkeypatch)

    assert automatic(tmp_path / "auto.sqlite3") == 0

    assert sentence in capsys.readouterr().out.splitlines()


def test_a_revoked_consent_leaves_an_automatic_run_with_only_a_count(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    route = groq_answers(respx_mock)
    path = tmp_path / "auto.sqlite3"
    consented(path, monkeypatch, capsys)
    assert consent(path, "auto-send", "3", "--yes") == 0
    capsys.readouterr()
    assert consent(path, "revoke") == 0
    capsys.readouterr()
    mailbox.add("a2")
    sent = route.call_count

    assert automatic(path) == 0

    assert "1 new message is ready to review; automatic analysis is off." in capsys.readouterr().out
    assert route.call_count == sent  # Nothing more was sent.
    assert consent(path, "status") == 0
    assert "Groq consent not granted" in capsys.readouterr().out


# With permission


def test_with_permission_it_sends_up_to_the_limit_and_defers_the_rest(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    route = groq_answers(respx_mock)
    path = tmp_path / "auto.sqlite3"
    consented(path, monkeypatch, capsys)  # a1 is analyzed and cached.
    assert consent(path, "auto-send", "1", "--yes") == 0
    capsys.readouterr()
    mailbox.add("a2")
    mailbox.add("a3")
    before = route.call_count

    assert automatic(path) == 0

    output = capsys.readouterr().out
    assert "Brief: saved (complete); items: 2" in output  # a1 reused, and one new message.
    assert (
        "Coverage: shortlisted 3, analyzed 1, reused 1, failed 0, skipped 0, deferred 1; "
        "sync complete: yes" in output
    )
    assert "1 deferred to your next review." in output
    assert "AI: Groq / test-model; requests: 1;" in output
    assert route.call_count == before + 1
    assert len(sent_messages(route.calls.last.request)) == 1  # The cap, at the provider.
    assert "Saved. Bodies were not stored." in output


def test_a_permission_too_small_for_the_carried_messages_sends_nothing_and_says_so(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    route = groq_answers(respx_mock)
    path = tmp_path / "auto.sqlite3"
    consented(path, monkeypatch, capsys)
    mailbox.add("a2")
    assert run_brief(path, "--yes") == 0  # a1 and a2 are in the day's brief.
    assert consent(path, "auto-send", "1", "--yes") == 0
    mailbox.add("a3")
    # Another model: a1 and a2 need analysis again, and the permission covers one message.
    monkeypatch.setenv("MAILBRIEF_GROQ_MODEL", "another-model")
    capsys.readouterr()
    sent = route.call_count

    assert automatic(path) == 0

    output = capsys.readouterr().out
    assert "Today's brief needs your review: 3 messages need analysis." in output.splitlines()
    assert "ready to review" not in output and "Brief:" not in output
    assert route.call_count == sent  # Nothing was sent, and the day's brief is as it was.
    assert gmail.main(["briefs", "show", "2026-09-16", "--database", str(path)]) == 0
    assert "Brief for 2026-09-16 (me@example.com): complete; items: 2" in capsys.readouterr().out


def test_turning_the_permission_off_takes_effect_on_the_next_run(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    route = groq_answers(respx_mock)
    path = tmp_path / "auto.sqlite3"
    consented(path, monkeypatch, capsys)
    assert consent(path, "auto-send", "2", "--yes") == 0
    mailbox.add("a2")
    capsys.readouterr()
    assert automatic(path) == 0  # Sends a2.
    sent = route.call_count
    mailbox.add("a3")
    capsys.readouterr()

    assert consent(path, "auto-send", "0") == 0  # No prompt to turn it off.
    assert capsys.readouterr().out == "Automatic analysis is off. Every run asks you first.\n"
    assert automatic(path) == 0

    # a1 and a2 are in the day's brief; only a3 is new.
    assert "1 new message is ready to review; automatic analysis is off." in (
        capsys.readouterr().out
    )
    assert route.call_count == sent


def test_an_automatic_run_can_show_what_it_saved(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    groq_answers(respx_mock)
    path = tmp_path / "auto.sqlite3"
    consented(path, monkeypatch, capsys)
    assert consent(path, "auto-send", "2", "--yes") == 0
    mailbox.add("a2")
    capsys.readouterr()

    assert automatic(path, "--show") == 0

    output = capsys.readouterr().out
    assert "sender@example.com: Approval needed" in output
    assert "A short summary." in output
    assert "evidence" not in output.lower()


@pytest.mark.parametrize(
    ("options", "flags"),
    [
        (("--date", "2026-09-15"), "--date"),
        (("--include", "a1"), "--include"),
        (("--exclude", "a1"), "--exclude"),
        (("--yes",), "--yes"),
        (("--yes", "--date", "2026-09-15"), "--date, --yes"),
    ],
)
def test_an_automatic_run_takes_no_date_choices_or_yes(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    capsys: pytest.CaptureFixture[str],
    options: tuple[str, ...],
    flags: str,
) -> None:
    path = tmp_path / "auto.sqlite3"

    with pytest.raises(SystemExit) as caught:
        automatic(path, *options)

    assert caught.value.code == 2
    assert f"--automatic can't be used with {flags}" in capsys.readouterr().err
    assert not path.exists()  # Nothing ran, and nothing was contacted.


# ai-consent auto-send


def test_the_permission_needs_a_consent_first(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "none.sqlite3"

    assert consent(path, "auto-send", "3", "--yes") == 3
    assert capsys.readouterr().out.strip() == NO_CONSENT
    # Turning it off always works: with no consent there is nothing to turn off.
    assert consent(path, "auto-send", "0") == 0
    assert capsys.readouterr().out.strip() == "Automatic analysis is already off."


def test_granting_shows_the_disclosure_and_asks_for_a_typed_yes(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    groq_answers(respx_mock)
    path = tmp_path / "auto.sqlite3"
    consented(path, monkeypatch, capsys)
    prompts = replies(monkeypatch, "yes")

    assert consent(path, "auto-send", "3") == 0

    output = capsys.readouterr().out
    assert "MailBrief will send 3 messages to Groq (test-model) for analysis." in output
    assert "plain-text body, cut to at most 4,000 characters" in output
    assert "Zero Data Retention" in output
    assert "Automatic runs may send up to 3 messages from me@example.com to Groq" in output
    assert "without asking. Revoke consent in Settings to stop." in output
    assert "Saved. Automatic runs may now send up to 3 messages without asking." in output
    assert prompts == ['Type "yes" to allow this: ']
    assert consent(path, "status") == 0
    assert "automatic analysis up to 3 messages per run since 2026-09-16" in capsys.readouterr().out


@pytest.mark.parametrize("answer", ["no", "", "y", "YES please", "sure"])
def test_anything_but_a_typed_yes_changes_nothing(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answer: str,
) -> None:
    groq_answers(respx_mock)
    path = tmp_path / "auto.sqlite3"
    consented(path, monkeypatch, capsys)
    replies(monkeypatch, answer)

    assert consent(path, "auto-send", "3") == 6

    assert "Nothing was changed." in capsys.readouterr().out
    assert consent(path, "status") == 0
    assert "automatic analysis off" in capsys.readouterr().out


def test_yes_skips_the_question_but_not_the_disclosure(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    groq_answers(respx_mock)
    path = tmp_path / "auto.sqlite3"
    consented(path, monkeypatch, capsys)
    replies(monkeypatch)  # A prompt would fail the test.

    assert consent(path, "auto-send", "1", "--yes") == 0

    output = capsys.readouterr().out
    assert "MailBrief will send 1 message to Groq" in output
    assert "may send up to 1 message from me@example.com" in output


@pytest.mark.parametrize("limit", ["11", "-1", "many", "2.5", "100"])
def test_a_limit_outside_zero_to_ten_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], limit: str
) -> None:
    with pytest.raises(SystemExit) as caught:
        consent(tmp_path / "x.sqlite3", "auto-send", limit)

    assert caught.value.code == 2
    assert "use a whole number from 0 to 10" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("auto-send",), "auto-send needs a number of messages, 0 to 10"),
        (("status", "3"), "status takes no number and no --yes"),
        (("revoke", "--yes"), "revoke takes no number and no --yes"),
    ],
)
def test_the_wrong_arguments_for_an_action_are_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], arguments: tuple[str, ...], message: str
) -> None:
    with pytest.raises(SystemExit) as caught:
        consent(tmp_path / "x.sqlite3", *arguments)

    assert caught.value.code == 2
    assert message in capsys.readouterr().err


def test_a_permission_can_not_be_granted_while_preferences_or_settings_are_unreadable(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    groq_answers(respx_mock)
    path = tmp_path / "auto.sqlite3"
    consented(path, monkeypatch, capsys)
    monkeypatch.setenv("MAILBRIEF_AI_BATCH_SIZE", "many")
    replies(monkeypatch)

    assert consent(path, "auto-send", "2", "--yes") == 3
    assert "Invalid setting: MAILBRIEF_AI_BATCH_SIZE" in capsys.readouterr().out
    assert consent(path, "auto-send", "0") == 0  # Turning it off needs no settings.
    monkeypatch.delenv("MAILBRIEF_AI_BATCH_SIZE")
    capsys.readouterr()

    save_preferences(path, PreferencesEdit(excluded_senders=("@example.com",)))
    corrupt(path)
    assert consent(path, "auto-send", "2", "--yes") == 3
    assert "Saved preferences could not be read" in capsys.readouterr().out
    assert consent(path, "auto-send", "0") == 0


# Status and preferences


def test_status_and_preferences_show_the_permission(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    groq_answers(respx_mock)
    path = tmp_path / "auto.sqlite3"
    save_preferences(path, PreferencesEdit(refresh_on_launch=True, refresh_interval_minutes=120))
    show = ["preferences", "show", "--database", str(path)]

    assert gmail.main(show) == 0
    output = capsys.readouterr().out
    assert "Refresh when MailBrief starts: yes\n" in output
    assert "Refresh while running: every 2 hours\n" in output
    assert (
        "Automatic analysis: unavailable until you give consent (analyze once with Sync and "
        "review)\n" in output
    )

    consented(path, monkeypatch, capsys)
    assert gmail.main(show) == 0
    assert "Automatic analysis: off, every run asks you first\n" in capsys.readouterr().out

    assert consent(path, "auto-send", "4", "--yes") == 0
    capsys.readouterr()
    assert gmail.main(show) == 0
    assert "Automatic analysis: up to 4 messages per run, since 2026-09-16" in (
        capsys.readouterr().out
    )
    assert consent(path, "status") == 0
    assert "Groq consent granted" in capsys.readouterr().out

    assert consent(path, "revoke") == 0
    capsys.readouterr()
    assert gmail.main(show) == 0
    assert "Automatic analysis: unavailable until you give consent" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("minutes", "text"),
    [(None, "off"), (60, "every hour"), (120, "every 2 hours"), (240, "every 4 hours")],
)
def test_preferences_show_names_each_refresh_interval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], minutes: int | None, text: str
) -> None:
    path = tmp_path / "prefs.sqlite3"
    save_preferences(path, PreferencesEdit(refresh_interval_minutes=minutes))

    assert gmail.main(["preferences", "show", "--database", str(path)]) == 0

    output = capsys.readouterr().out
    assert f"Refresh while running: {text}\n" in output
    assert "Refresh when MailBrief starts: no\n" in output


# Declines


def test_exclude_remembers_a_decline_that_include_takes_back(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "declines.sqlite3"
    mailbox.add("a2")

    assert sync(path, "--exclude", "a1") == 0
    capsys.readouterr()

    assert sync(path, "--show-metadata") == 0
    lines = capsys.readouterr().out.splitlines()
    (index,) = (number for number, line in enumerate(lines) if line.startswith("a1 ["))
    assert lines[index].startswith("a1 [omitted]")  # Skipped, though nothing excluded it now.
    assert "  you left this out earlier" in lines[index : index + 4]
    (other,) = (number for number, line in enumerate(lines) if line.startswith("a2 ["))
    assert "you left this out earlier" not in " ".join(lines[other : other + 4])

    assert sync(path, "--include", "a1") == 0  # Selecting it again forgets the decline.
    capsys.readouterr()
    assert sync(path, "--show-metadata") == 0
    output = capsys.readouterr().out
    assert "you left this out earlier" not in output and "a1 [selected]" in output


def test_a_declined_message_is_left_out_of_automatic_runs(
    tmp_path: Path,
    mailbox: Mailbox,  # noqa: F811 - the imported fixture
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    route = groq_answers(respx_mock)
    path = tmp_path / "auto.sqlite3"
    consented(path, monkeypatch, capsys)
    assert consent(path, "auto-send", "5", "--yes") == 0
    mailbox.add("a2")
    capsys.readouterr()
    assert sync(path, "--exclude", "a2") == 0  # Left out by hand: remembered.
    capsys.readouterr()
    sent = route.call_count

    assert automatic(path) == 0

    output = capsys.readouterr().out
    assert "Nothing new to review." in output  # a1 is in the day's brief; a2 was left out.
    assert route.call_count == sent
