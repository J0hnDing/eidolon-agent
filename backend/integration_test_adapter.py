"""Deterministic credential-free adapter for generated skill tests only."""

from copy import deepcopy
from typing import Any


class FakeIntegrationError(RuntimeError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


class DeterministicFakeIntegrationAdapter:
    def __init__(
        self,
        responses: dict[str, dict[str, Any]],
        *,
        failures: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        self._responses = deepcopy(responses)
        self._failures = dict(failures or {})
        self.calls: list[dict[str, Any]] = []

    def call(
        self,
        *,
        operation: str,
        input: dict[str, Any],  # noqa: A002 - matches stable runtime helper
        timeout_seconds: float = 30,
    ) -> dict[str, Any]:
        del timeout_seconds
        self.calls.append({"operation": operation, "input": deepcopy(input)})
        if operation in self._failures:
            error_type, message = self._failures[operation]
            raise FakeIntegrationError(error_type, message)
        if operation not in self._responses:
            raise FakeIntegrationError("operation_undeclared", "Fake operation was not selected for this test")
        return deepcopy(self._responses[operation])
