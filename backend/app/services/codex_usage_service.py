from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.services.codex_app_server import (
    CodexAppServerClient,
    CodexAppServerError,
    codex_app_server_client,
)


class CodexUsageService:
    """Persistent, local Codex App Server client for account allowance data."""

    def __init__(
        self,
        command: str | None = None,
        *,
        client: CodexAppServerClient | None = None,
    ) -> None:
        self.client = client or CodexAppServerClient(command)
        self._last_error: str | None = None
        self._model_catalog: dict[str, Any] | None = None
        self._model_catalog_expires_at: datetime | None = None

    def start(self) -> None:
        try:
            self.client.start()
        except (OSError, RuntimeError, CodexAppServerError, ValueError) as exc:
            self._last_error = str(exc)

    def stop(self) -> None:
        self.client.stop()

    def read_account_usage(self) -> dict[str, Any]:
        try:
            payload = self._request("account/rateLimits/read", None)
            snapshot = self._select_codex_snapshot(payload)
            return {
                "available": True,
                "source": "codex_app_server",
                "fetched_at": datetime.now(UTC).isoformat(),
                "plan_type": snapshot.get("planType"),
                "limit_id": snapshot.get("limitId") or "codex",
                "rate_limit_reached_type": snapshot.get("rateLimitReachedType"),
                "five_hour": self._window(snapshot.get("primary"), "5-hour"),
                "weekly": self._window(snapshot.get("secondary"), "weekly"),
            }
        except (OSError, RuntimeError, CodexAppServerError, ValueError) as exc:
            self._last_error = str(exc)
            return {
                "available": False,
                "source": "codex_app_server",
                "fetched_at": datetime.now(UTC).isoformat(),
                "error": self._last_error,
                "plan_type": None,
                "limit_id": "codex",
                "rate_limit_reached_type": None,
                "five_hour": None,
                "weekly": None,
            }

    def should_pause_workflow(self, minimum_remaining_percent: int = 5) -> tuple[bool, dict[str, Any]]:
        usage = self.read_account_usage()
        if not usage["available"]:
            return False, usage
        windows = [usage.get("five_hour"), usage.get("weekly")]
        below_reserve = bool(usage.get("rate_limit_reached_type")) or any(
            window and window.get("remaining_percent", 100) < minimum_remaining_percent for window in windows
        )
        return below_reserve, usage

    def read_model_catalog(self, *, refresh: bool = False) -> dict[str, Any]:
        now = datetime.now(UTC)
        if (
            not refresh
            and self._model_catalog is not None
            and self._model_catalog_expires_at is not None
            and now < self._model_catalog_expires_at
        ):
            return self._model_catalog
        try:
            models: list[dict[str, Any]] = []
            cursor: str | None = None
            while True:
                result = self._request(
                    "model/list",
                    {"cursor": cursor, "includeHidden": False, "limit": 100},
                )
                data = result.get("data")
                if not isinstance(data, list):
                    raise RuntimeError("Codex App Server returned an invalid model catalog")
                models.extend(self._normalize_model(item) for item in data if isinstance(item, dict))
                next_cursor = result.get("nextCursor")
                if not isinstance(next_cursor, str) or not next_cursor:
                    break
                cursor = next_cursor
            catalog = {
                "available": True,
                "fetched_at": now.isoformat(),
                "error": None,
                "models": models,
            }
            self._model_catalog = catalog
            self._model_catalog_expires_at = now + timedelta(minutes=5)
            return catalog
        except (OSError, RuntimeError, CodexAppServerError, ValueError) as exc:
            return {
                "available": False,
                "fetched_at": now.isoformat(),
                "error": str(exc),
                "models": [],
            }

    def _request(self, method: str, params: Any) -> dict[str, Any]:
        return self.client.request(method, params)

    @staticmethod
    def _select_codex_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
        by_id = payload.get("rateLimitsByLimitId")
        if isinstance(by_id, dict) and isinstance(by_id.get("codex"), dict):
            return by_id["codex"]
        snapshot = payload.get("rateLimits")
        if not isinstance(snapshot, dict):
            raise ValueError("Codex allowance data is unavailable")
        return snapshot

    @staticmethod
    def _normalize_model(value: dict[str, Any]) -> dict[str, Any]:
        effort_values = []
        for option in value.get("supportedReasoningEfforts", []):
            effort = option.get("reasoningEffort") if isinstance(option, dict) else None
            if isinstance(effort, str) and effort and effort not in effort_values:
                effort_values.append(effort)
        default_effort = value.get("defaultReasoningEffort")
        if isinstance(default_effort, str) and default_effort and default_effort not in effort_values:
            effort_values.append(default_effort)
        model = str(value.get("model") or value.get("id") or "")
        return {
            "id": str(value.get("id") or model),
            "model": model,
            "display_name": str(value.get("displayName") or model),
            "description": str(value.get("description") or ""),
            "is_default": bool(value.get("isDefault")),
            "default_reasoning_effort": str(default_effort or "medium"),
            "supported_reasoning_efforts": effort_values,
        }

    @staticmethod
    def _window(value: Any, label: str) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        used = max(0, min(100, int(value.get("usedPercent", 0))))
        resets_at = value.get("resetsAt")
        return {
            "label": label,
            "used_percent": used,
            "remaining_percent": 100 - used,
            "window_duration_minutes": value.get("windowDurationMins"),
            "resets_at": datetime.fromtimestamp(resets_at, UTC).isoformat() if isinstance(resets_at, int) else None,
        }


codex_usage_service = CodexUsageService(client=codex_app_server_client)
