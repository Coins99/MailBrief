"""Deterministic local ranking service for email metadata."""

import re
from collections.abc import Sequence
from datetime import datetime, timedelta

from mailbrief.domain.common import normalize_utc
from mailbrief.domain.messages import (
    MessageImportance,
    NormalizedMessage,
    RankedMessage,
    RankReason,
)

DEFAULT_THRESHOLD: int = 10
MIN_SHORTLIST_SIZE: int = 3
MAX_SHORTLIST_SIZE: int = 10

_ACTION_PATTERNS: list[str] = [
    r"\baction required\b",
    r"\baction needed\b",
    r"\baction item\b",
    r"\bdeadline\b",
    r"\bdue date\b",
    r"\bdue today\b",
    r"\bdue tomorrow\b",
    r"\bdue soon\b",
    r"\bdue by\b",
    r"\bdue\b",
    r"\burgent\b",
    r"\basap\b",
    r"\beod\b",
    r"\bcob\b",
    r"\bby (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|today|tomorrow|eod|cob)\b",
]
_ACTION_RE = re.compile("|".join(_ACTION_PATTERNS), re.IGNORECASE)

_APPROVAL_PATTERNS: list[str] = [
    r"\bplease approve\b",
    r"\bapproval needed\b",
    r"\bapproval required\b",
    r"\bapproval\b",
    r"\bapprove\b",
    r"\bplease review\b",
    r"\breview\b",
    r"\bplease confirm\b",
    r"\bconfirm\b",
    r"\bplease reply\b",
    r"\breply needed\b",
    r"\breply required\b",
    r"\bresponse needed\b",
    r"\bresponse required\b",
    r"\bsign[- ]off\b",
    r"\bcan you\b",
    r"\bcould you\b",
    r"\bplease send\b",
    r"\bneeds your\b",
    r"\bwaiting on\b",
    r"\bwaiting for\b",
]
_APPROVAL_RE = re.compile("|".join(_APPROVAL_PATTERNS), re.IGNORECASE)

_AUTOMATED_SENDER_TOKENS: frozenset[str] = frozenset(
    {
        "no-reply",
        "noreply",
        "do-not-reply",
        "donotreply",
        "no_reply",
        "no.reply",
    }
)


def _normalize_text(text: str) -> str:
    """Normalize whitespace runs and lowercase text for token matching."""
    return re.sub(r"\s+", " ", text.lower()).strip()


def _is_automated_sender(sender_address: str) -> bool:
    """Check if the sender email address indicates an automated system."""
    if not sender_address or "@" not in sender_address:
        return False
    local_part = sender_address.split("@", maxsplit=1)[0].lower()
    return any(token in local_part for token in _AUTOMATED_SENDER_TOKENS)


def score_message(
    msg: NormalizedMessage,
    *,
    user_email: str | Sequence[str] | set[str] | frozenset[str],
    now_utc: datetime,
) -> tuple[int, tuple[RankReason, ...]]:
    """Compute deterministic local score and reason list for one message.

    Applies the 10 scoring rules specified in mvp-plan.md Section 9 in strict declaration order.
    """
    score = 0
    reasons: list[RankReason] = []

    # 1. High importance (+20)
    if msg.importance == MessageImportance.HIGH:
        score += 20
        reasons.append(RankReason.HIGH_IMPORTANCE)

    # 2. Low importance (-10)
    elif msg.importance == MessageImportance.LOW:
        score -= 10
        reasons.append(RankReason.LOW_IMPORTANCE)

    # 3. Unread (+8)
    if not msg.is_read:
        score += 8
        reasons.append(RankReason.UNREAD)

    # 4. User is directly in to_recipients (+8)
    if isinstance(user_email, str):
        target_addresses = {user_email.strip().lower()}
    else:
        target_addresses = {a.strip().lower() for a in user_email if a}

    is_direct = any(r.address.strip().lower() in target_addresses for r in msg.to_recipients)
    if is_direct:
        score += 8
        reasons.append(RankReason.DIRECT_RECIPIENT)

    # 5. Has attachments (+3)
    if msg.has_attachments:
        score += 3
        reasons.append(RankReason.HAS_ATTACHMENTS)

    # Search subject and preview for keyword rules
    haystack = _normalize_text(f"{msg.subject} {msg.body_preview}")

    # 6. Action or deadline terms (+20)
    if _ACTION_RE.search(haystack):
        score += 20
        reasons.append(RankReason.ACTION_OR_DEADLINE_LANGUAGE)

    # 7. Approval, reply, or request terms (+10)
    if _APPROVAL_RE.search(haystack):
        score += 10
        reasons.append(RankReason.APPROVAL_OR_REPLY_LANGUAGE)

    # Recency scoring (mutually exclusive)
    aware_received = normalize_utc(msg.received_at_utc)
    aware_now = normalize_utc(now_utc)
    age = max(timedelta(0), aware_now - aware_received)

    # 8. Very recent <= 3 hours (+8)
    if age <= timedelta(hours=3):
        score += 8
        reasons.append(RankReason.VERY_RECENT)
    # 9. Recent > 3 hours and <= 8 hours (+5)
    elif age <= timedelta(hours=8):
        score += 5
        reasons.append(RankReason.RECENT)

    # 10. Automated sender (-12)
    if _is_automated_sender(msg.sender.address):
        score -= 12
        reasons.append(RankReason.AUTOMATED_SENDER)

    return score, tuple(reasons)


def rank_messages(
    messages: Sequence[NormalizedMessage],
    *,
    user_email: str | Sequence[str] | set[str] | frozenset[str],
    now_utc: datetime,
) -> list[RankedMessage]:
    """Score a collection of normalized messages with a shared reference timestamp."""
    ranked: list[RankedMessage] = []
    for msg in messages:
        score, reasons = score_message(msg, user_email=user_email, now_utc=now_utc)
        ranked.append(RankedMessage(message=msg, score=score, reasons=reasons))
    return ranked


def _sort_key(m: RankedMessage) -> tuple[int, int, str]:
    """Deterministic total sorting order: score DESC, received_at DESC, provider_id ASC."""
    ts_us = int(normalize_utc(m.message.received_at_utc).timestamp() * 1_000_000)
    return (-m.score, -ts_us, m.message.provider_message_id)


def select_shortlist(
    ranked: Sequence[RankedMessage],
    *,
    threshold: int = DEFAULT_THRESHOLD,
    min_size: int = MIN_SHORTLIST_SIZE,
    max_size: int = MAX_SHORTLIST_SIZE,
) -> list[RankedMessage]:
    """Select the deterministic shortlist of messages.

    1. Sort all messages by (-score, -timestamp_us, provider_message_id ASC).
    2. Filter messages meeting the qualification threshold (score >= threshold).
    3. If >= min_size qualify, return top max_size qualifiers.
    4. Otherwise, backfill from sorted_all until min_size (or all available) is reached.
    """
    if not ranked:
        return []

    sorted_all = sorted(ranked, key=_sort_key)
    qualified = [m for m in sorted_all if m.score >= threshold]

    if len(qualified) >= min_size:
        return qualified[:max_size]

    return sorted_all[: min(min_size, len(sorted_all))]


def review_shortlist(
    ranked: Sequence[RankedMessage],
    *,
    include_ids: tuple[str, ...] = (),
    exclude_ids: tuple[str, ...] = (),
) -> list[RankedMessage]:
    """Apply explicit choices only within this account/day; do not backfill exclusions."""
    include, exclude = set(include_ids), set(exclude_ids)
    available = {item.message.provider_message_id: item for item in ranked}
    if include & exclude or (include | exclude) - available.keys():
        raise ValueError("Review IDs must belong to this Inbox window and cannot overlap.")
    if len(include) > MAX_SHORTLIST_SIZE:
        raise ValueError("Too many manually included messages for one shortlist.")
    automatic = select_shortlist(ranked)
    selected = [available[key] for key in include]
    selected.extend(
        item for item in automatic if item.message.provider_message_id not in include | exclude
    )
    return sorted(selected[:MAX_SHORTLIST_SIZE], key=_sort_key)
