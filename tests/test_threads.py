"""Thread stores: history is per user and ordered; a foreign thread is invisible and read-only."""

import pytest

from app.adapters.metadata.threads import InMemoryThreadStore
from app.ports.threads import Turn


def test_history_returns_the_newest_turns_oldest_first() -> None:
    store = InMemoryThreadStore()
    for n in range(5):
        store.append("t", "u1", "p", Turn(f"q{n}", f"s{n}"))
    assert [t.question for t in store.history("t", "u1", limit=3)] == ["q2", "q3", "q4"]


def test_another_user_sees_nothing_and_cannot_append() -> None:
    store = InMemoryThreadStore()
    store.append("t", "u1", "p", Turn("q", "s"))
    assert store.history("t", "u2", limit=5) == []
    with pytest.raises(PermissionError):
        store.append("t", "u2", "p", Turn("x", "x"))
    assert len(store.history("t", "u1", limit=5)) == 1
