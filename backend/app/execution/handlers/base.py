from __future__ import annotations

from typing import Protocol

from app.execution.context import InvocationContext
from app.execution.types import InvocationOutcome, InvocationTargetRef
from app.models import InvocationApproval


class InvocationHandler(Protocol):
    def execute(
        self,
        target: InvocationTargetRef,
        input_json: dict,
        context: InvocationContext,
    ) -> InvocationOutcome: ...

    def execute_approved(
        self,
        approval: InvocationApproval,
        context: InvocationContext,
    ) -> InvocationOutcome: ...
