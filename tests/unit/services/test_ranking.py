"""Unit tests for deterministic local ranking and shortlist selection."""

import random
from datetime import UTC, datetime, timedelta

from mailbrief.domain.messages import (
    EmailContact,
    MessageImportance,
    NormalizedMessage,
    RankedMessage,
    RankReason,
)
from mailbrief.services.ranking import (
    rank_messages,
    score_message,
    select_shortlist,
)
from tests.factories import make_message

NOW = datetime(2026, 8, 31, 18, 0, 0, tzinfo=UTC)
USER_EMAIL = "taylor@example.com"


def make_baseline_message(**overrides: object) -> NormalizedMessage:
    """Build a neutral message that scores exactly 0 points with no reasons."""
    values: dict[str, object] = {
        "provider_message_id": "msg-neutral",
        "subject": "General update",
        "body_preview": "Here is some general information for the team.",
        "sender": EmailContact(name="Alex", address="alex@example.com"),
        "to_recipients": (EmailContact(name="Other", address="other@example.com"),),
        "received_at_utc": NOW - timedelta(hours=12),  # > 8h ago
        "is_read": True,
        "importance": MessageImportance.NORMAL,
        "has_attachments": False,
    }
    values.update(overrides)
    return make_message(**values)


def test_baseline_scores_zero() -> None:
    msg = make_baseline_message()
    score, reasons = score_message(msg, user_email=USER_EMAIL, now_utc=NOW)
    assert score == 0
    assert reasons == ()


def test_rule_1_high_importance() -> None:
    msg = make_baseline_message(importance=MessageImportance.HIGH)
    score, reasons = score_message(msg, user_email=USER_EMAIL, now_utc=NOW)
    assert score == 20
    assert reasons == (RankReason.HIGH_IMPORTANCE,)


def test_rule_2_low_importance() -> None:
    msg = make_baseline_message(importance=MessageImportance.LOW)
    score, reasons = score_message(msg, user_email=USER_EMAIL, now_utc=NOW)
    assert score == -10
    assert reasons == (RankReason.LOW_IMPORTANCE,)


def test_rule_3_unread() -> None:
    msg = make_baseline_message(is_read=False)
    score, reasons = score_message(msg, user_email=USER_EMAIL, now_utc=NOW)
    assert score == 8
    assert reasons == (RankReason.UNREAD,)


def test_rule_4_direct_recipient() -> None:
    msg = make_baseline_message(
        to_recipients=(EmailContact(name="Taylor", address="Taylor@Example.com"),)
    )
    score, reasons = score_message(msg, user_email=USER_EMAIL, now_utc=NOW)
    assert score == 8
    assert reasons == (RankReason.DIRECT_RECIPIENT,)


def test_rule_5_has_attachments() -> None:
    msg = make_baseline_message(has_attachments=True)
    score, reasons = score_message(msg, user_email=USER_EMAIL, now_utc=NOW)
    assert score == 3
    assert reasons == (RankReason.HAS_ATTACHMENTS,)


def test_rule_6_action_deadline_language() -> None:
    msg = make_baseline_message(subject="Action Required: Project milestone due by Friday")
    score, reasons = score_message(msg, user_email=USER_EMAIL, now_utc=NOW)
    assert score == 20
    assert reasons == (RankReason.ACTION_OR_DEADLINE_LANGUAGE,)


def test_rule_7_approval_reply_request_language() -> None:
    msg = make_baseline_message(body_preview="Could you please review and confirm your approval?")
    score, reasons = score_message(msg, user_email=USER_EMAIL, now_utc=NOW)
    assert score == 10
    assert reasons == (RankReason.APPROVAL_OR_REPLY_LANGUAGE,)


def test_rule_8_very_recent() -> None:
    msg = make_baseline_message(received_at_utc=NOW - timedelta(hours=2))
    score, reasons = score_message(msg, user_email=USER_EMAIL, now_utc=NOW)
    assert score == 8
    assert reasons == (RankReason.VERY_RECENT,)


def test_rule_9_recent() -> None:
    msg = make_baseline_message(received_at_utc=NOW - timedelta(hours=5))
    score, reasons = score_message(msg, user_email=USER_EMAIL, now_utc=NOW)
    assert score == 5
    assert reasons == (RankReason.RECENT,)


def test_rule_10_automated_sender() -> None:
    msg = make_baseline_message(sender=EmailContact(name="Alerts", address="no-reply@company.com"))
    score, reasons = score_message(msg, user_email=USER_EMAIL, now_utc=NOW)
    assert score == -12
    assert reasons == (RankReason.AUTOMATED_SENDER,)


def test_recency_boundaries() -> None:
    # Exactly 3 hours: VERY_RECENT (+8)
    msg_3h = make_baseline_message(received_at_utc=NOW - timedelta(hours=3))
    assert score_message(msg_3h, user_email=USER_EMAIL, now_utc=NOW) == (
        8,
        (RankReason.VERY_RECENT,),
    )

    # 3 hours + 1 second: RECENT (+5)
    msg_3h_1s = make_baseline_message(received_at_utc=NOW - timedelta(hours=3, seconds=1))
    assert score_message(msg_3h_1s, user_email=USER_EMAIL, now_utc=NOW) == (5, (RankReason.RECENT,))

    # Exactly 8 hours: RECENT (+5)
    msg_8h = make_baseline_message(received_at_utc=NOW - timedelta(hours=8))
    assert score_message(msg_8h, user_email=USER_EMAIL, now_utc=NOW) == (5, (RankReason.RECENT,))

    # 8 hours + 1 second: No recency adjustment (0)
    msg_8h_1s = make_baseline_message(received_at_utc=NOW - timedelta(hours=8, seconds=1))
    assert score_message(msg_8h_1s, user_email=USER_EMAIL, now_utc=NOW) == (0, ())

    # Future message / clock skew: clamped to 0 -> VERY_RECENT (+8)
    msg_future = make_baseline_message(received_at_utc=NOW + timedelta(minutes=5))
    assert score_message(msg_future, user_email=USER_EMAIL, now_utc=NOW) == (
        8,
        (RankReason.VERY_RECENT,),
    )


def test_automated_sender_tokens() -> None:
    tokens = [
        "noreply@domain.com",
        "do-not-reply@domain.com",
        "notifications_no_reply@domain.com",
        "no.reply@domain.com",
    ]
    for addr in tokens:
        msg = make_baseline_message(sender=EmailContact(name="Bot", address=addr))
        score, reasons = score_message(msg, user_email=USER_EMAIL, now_utc=NOW)
        assert score == -12
        assert reasons == (RankReason.AUTOMATED_SENDER,)

    # Human sender with "noreen" should NOT match
    human_msg = make_baseline_message(
        sender=EmailContact(name="Noreen", address="noreen@domain.com")
    )
    assert score_message(human_msg, user_email=USER_EMAIL, now_utc=NOW) == (0, ())


def test_combination_and_reason_order() -> None:
    # High importance (+20), unread (+8), direct (+8), action lang (+20), very recent (+8)
    # Total = 64
    msg = make_baseline_message(
        importance=MessageImportance.HIGH,
        is_read=False,
        to_recipients=(EmailContact(name="Taylor", address=USER_EMAIL),),
        subject="Urgent action required",
        received_at_utc=NOW - timedelta(hours=1),
    )
    score, reasons = score_message(msg, user_email=USER_EMAIL, now_utc=NOW)
    assert score == 64
    assert reasons == (
        RankReason.HIGH_IMPORTANCE,
        RankReason.UNREAD,
        RankReason.DIRECT_RECIPIENT,
        RankReason.ACTION_OR_DEADLINE_LANGUAGE,
        RankReason.VERY_RECENT,
    )


def test_deterministic_sorting_and_tie_breaking() -> None:
    # Build multiple messages with intentional ties in score and received time
    t1 = NOW - timedelta(hours=1)
    t2 = NOW - timedelta(hours=2)

    m1 = RankedMessage(
        message=make_baseline_message(provider_message_id="msg-B", received_at_utc=t1), score=20
    )
    m2 = RankedMessage(
        message=make_baseline_message(provider_message_id="msg-A", received_at_utc=t1), score=20
    )
    m3 = RankedMessage(
        message=make_baseline_message(provider_message_id="msg-C", received_at_utc=t2), score=20
    )
    m4 = RankedMessage(
        message=make_baseline_message(provider_message_id="msg-D", received_at_utc=t1), score=30
    )

    # Expected order: m4 (score 30), then m2 (score 20, t1, id "msg-A"),
    # then m1 (score 20, t1, id "msg-B"), then m3 (score 20, t2)
    expected_ids = ["msg-D", "msg-A", "msg-B", "msg-C"]

    # Shuffling input 50 times must always yield the exact same order
    rng = random.Random(42)
    pool = [m1, m2, m3, m4]
    for _ in range(50):
        shuffled = list(pool)
        rng.shuffle(shuffled)
        shortlist = select_shortlist(shuffled, min_size=4, max_size=10)
        actual_ids = [m.message.provider_message_id for m in shortlist]
        assert actual_ids == expected_ids


def test_shortlist_quotas_and_fallback() -> None:
    # 0 messages -> []
    assert select_shortlist([]) == []

    # 2 messages total, both sub-threshold -> returns both (backfill to min_size=3)
    sub1 = RankedMessage(message=make_baseline_message(provider_message_id="sub-1"), score=5)
    sub2 = RankedMessage(message=make_baseline_message(provider_message_id="sub-2"), score=-2)
    assert [m.message.provider_message_id for m in select_shortlist([sub1, sub2])] == [
        "sub-1",
        "sub-2",
    ]

    # 1 qualifier (>=10) + 5 sub-threshold -> exactly 3 (1 qualifier + top 2 backfilled)
    q1 = RankedMessage(message=make_baseline_message(provider_message_id="q-1"), score=15)
    subs = [
        RankedMessage(message=make_baseline_message(provider_message_id=f"sub-{i}"), score=i)
        for i in range(1, 6)
    ]
    res = select_shortlist([q1, *subs])
    assert len(res) == 3
    assert res[0].message.provider_message_id == "q-1"
    assert res[1].message.provider_message_id == "sub-5"
    assert res[2].message.provider_message_id == "sub-4"

    # Exactly 3 qualifiers -> exactly 3
    q2 = RankedMessage(message=make_baseline_message(provider_message_id="q-2"), score=20)
    q3 = RankedMessage(message=make_baseline_message(provider_message_id="q-3"), score=12)
    res3 = select_shortlist([q1, q2, q3, *subs])
    assert len(res3) == 3
    assert [m.message.provider_message_id for m in res3] == ["q-2", "q-1", "q-3"]

    # 15 qualifiers -> capped at max_size=10
    fifteen = [
        RankedMessage(message=make_baseline_message(provider_message_id=f"q-{i:02d}"), score=10 + i)
        for i in range(15)
    ]
    res10 = select_shortlist(fifteen)
    assert len(res10) == 10
    assert res10[0].score == 24
    assert res10[-1].score == 15


def test_rank_messages_batch() -> None:
    msgs = [
        make_baseline_message(provider_message_id="msg-1", importance=MessageImportance.HIGH),
        make_baseline_message(provider_message_id="msg-2", is_read=False),
    ]
    ranked = rank_messages(msgs, user_email=USER_EMAIL, now_utc=NOW)
    assert len(ranked) == 2
    assert ranked[0].score == 20
    assert ranked[1].score == 8


def test_is_automated_sender_edge_cases() -> None:
    msg_empty_addr = make_baseline_message()
    # Test with invalid / empty address formats
    score, reasons = score_message(msg_empty_addr, user_email=USER_EMAIL, now_utc=NOW)
    assert RankReason.AUTOMATED_SENDER not in reasons
