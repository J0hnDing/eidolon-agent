from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

current_skill_id: ContextVar[int | None] = ContextVar("current_skill_id", default=None)


@contextmanager
def skill_execution_context(skill_id: int) -> Iterator[None]:
    token = current_skill_id.set(skill_id)
    try:
        yield
    finally:
        current_skill_id.reset(token)


def get_current_skill_id() -> int | None:
    return current_skill_id.get()
