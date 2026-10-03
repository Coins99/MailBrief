"""Reading a thread's message metadata, for tracking the threads of open actions (ADR 0015)."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from mailbrief.domain.messages import NormalizedMessage


@dataclass(frozen=True, slots=True)
class ThreadSnapshot:
    """One read of a thread: its messages, oldest first, and the provider message IDs of
    the ones the provider holds in Trash or Spam, which a cache must forget."""

    messages: tuple[NormalizedMessage, ...] = ()
    discarded_ids: frozenset[str] = frozenset()


@runtime_checkable
class ThreadReader(Protocol):
    """Reads one thread's message metadata; never bodies or attachments."""

    async def fetch_thread(self, provider_thread_id: str) -> ThreadSnapshot:
        """The thread's messages, oldest first, skipping drafts, trash and spam, and the
        IDs of its messages in Trash or Spam.

        Raises MessageUnavailableError when the thread no longer exists.
        """
        ...
