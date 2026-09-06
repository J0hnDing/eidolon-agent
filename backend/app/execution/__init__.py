"""Authenticated execution boundary for Eidolon catalog callables."""

from app.execution.context import InvocationContext
from app.execution.types import (
    InvocationExecutionError,
    InvocationOutcome,
    InvocationTargetRef,
)

__all__ = [
    "InvocationContext",
    "InvocationExecutionError",
    "InvocationOutcome",
    "InvocationTargetRef",
]
