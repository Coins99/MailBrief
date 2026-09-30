"""Reading a thread's message metadata, for tracking the threads of open actions (ADR 0015)."""

from typing import Protocol, runtime_checkable

from mailbrief.domain.messages import NormalizedMessage


@runtime_checkable
class ThreadReader(Protocol):
    """Reads one thread's message metadata; never bodies or attachments."""

    async def fetch_thread(self, provider_thread_id: str) -> tuple[NormalizedMessage, ...]:
        """The thread's messages, oldest first, skipping drafts, trash and spam.

        Raises MessageUnavailableError when the thread no longer exists.
        """
        ...
