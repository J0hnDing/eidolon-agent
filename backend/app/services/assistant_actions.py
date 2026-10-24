"""Reusable action validation and display for Assistant approval records."""
import hashlib
import json

from jsonschema import Draft202012Validator, ValidationError
from sqlalchemy.orm import Session

from app.execution.context_factory import InvocationContextFactory
from app.execution.executor import InvocationExecutor
from app.execution.types import InvocationTargetRef
from app.services.atlas_provider import UrllibAtlasProviderAdapter
from app.services.function_catalog_service import FunctionCatalogService


def deterministic_catalog(db: Session) -> dict[str, dict]:
    return {
        entry["id"]: entry
        for entry in FunctionCatalogService(db).list_entries(refresh=True)
        if entry.get("availability") == "available"
        and entry.get("mcp_exposed") is True
        and entry.get("agent_selectable", True) is not False
        and entry.get("requires_invocation_approval") is not True
        and entry.get("uses_codex") is not True
        and entry.get("category") in {"integration", "user", "backend_core"}
        and isinstance(entry.get("input_schema"), dict)
    }

def validate_action(action: dict, catalog: dict[str, dict], follow_up: dict | None = None) -> None:
    if action["type"] == "act":
        return
    for step in action["steps"]:
        entry = catalog.get(step["function"])
        if entry is None:
            raise ValueError(f"Function {step['function']} is unavailable for deterministic execution")
        try:
            Draft202012Validator(entry["input_schema"]).validate(step["arguments"])
        except ValidationError as exc:
            raise ValueError(f"Invalid arguments for {step['function']}: {exc.message[:200]}") from None
        if _has_placeholder(step["arguments"]):
            raise ValueError("Function arguments must be completely resolved")

def _has_placeholder(value: object) -> bool:
    if isinstance(value, dict):
        return any(_has_placeholder(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_placeholder(item) for item in value)
    return isinstance(value, str) and any(token in value.casefold() for token in (
        "{{", "}}", "<todo", "<goal", "<event", "tbd", "to be determined", "placeholder",
    ))

def proposal_fingerprint(action: dict, provenance: dict) -> str:
    return hashlib.sha256(json.dumps(
        {"action": action, "provenance": provenance},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()

def resolve_target_name(db: Session, target: dict) -> str:
    kind = target.get("type")
    record_id = target.get("id")
    fallback = "Todo" if kind == "todo" else "Goal"
    if not isinstance(record_id, str) or not record_id:
        return fallback
    try:
        if kind == "todo":
            executor = InvocationExecutor(db)
            context = InvocationContextFactory.trusted_system("assistant_reference_display")
            arguments = {"page_size": 100}
            for _ in range(100):
                outcome = executor.execute(
                    InvocationTargetRef(category="integration", target_id="notion.todo.list"),
                    arguments, context,
                )
                page = outcome.output or {}
                for todo in page.get("todos", []):
                    if todo.get("id") == record_id:
                        name = todo.get("title")
                        return name.strip() if isinstance(name, str) and name.strip() else fallback
                if not page.get("has_more") or not page.get("next_cursor"):
                    break
                arguments["start_cursor"] = page["next_cursor"]
            return fallback
        provider = UrllibAtlasProviderAdapter()
        records = provider._record_list(provider._request(
            "/api/records?category=goal", None, timeout=10,
            max_bytes=2_000_000, method="GET",
        ), "goal")
        for record in records:
            if record.get("id") == record_id:
                name = record.get("title")
                return name.strip() if isinstance(name, str) and name.strip() else fallback
        return fallback
    except Exception:
        return fallback
