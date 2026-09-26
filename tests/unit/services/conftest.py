"""Service-test hooks: every scripted AI answer must be used."""

from collections.abc import Iterator

import pytest

from tests.unit.services.ai_fakes import FakeAIProvider


@pytest.fixture(autouse=True)
def ai_scripts_are_used_up() -> Iterator[None]:
    """Fail a test whose FakeAIProvider still holds script items it never consumed."""
    FakeAIProvider.created.clear()
    yield
    leftovers = [len(provider.script) for provider in FakeAIProvider.created if provider.script]
    FakeAIProvider.created.clear()
    assert not leftovers, f"FakeAIProvider script items left unused: {leftovers}"
