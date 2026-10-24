"""Fixed, read-only Assistant sources. Snapshots retain metadata, never source bodies."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.execution.context import InvocationContext
from app.models import AgentPolicy, AssistantState, IntegrationConnection
from app.services.agent_policy_service import AgentPermissionError, AgentPolicyService
from app.services.atlas_lifecycle_service import atlas_lifecycle_service
from app.services.integration_service import IntegrationError, build_default_integration_service

_MAX_PAGES = 100
_MAX_ITEMS = 10000
_SOURCE_READS = {
    "atlas.records": ("atlas", ("atlas.records.search",)),
    "atlas.links": ("atlas", ("atlas.records.search", "atlas.records.get")),
    "notion.todos": ("notion", ("notion.todo.list",)),
    "calendar.events": ("google_calendar", ("google_calendar.event.list",)),
    "email.gmail": ("gmail", ("email.search",)),
    "email.outlook": ("outlook", ("email.search",)),
}


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _unexpired(value: str | None, now: datetime) -> bool:
    if value is None:
        return True
    expires = datetime.fromisoformat(value)
    expires = expires.astimezone(UTC) if expires.tzinfo else expires.replace(tzinfo=UTC)
    return expires > now


def _source_key(db: Session, service, name: str, provider: str) -> str:
    if provider == "atlas":
        identity = str(atlas_lifecycle_service.directory)
    else:
        connection = service._connection(provider)
        if isinstance(connection, IntegrationConnection):
            db.refresh(connection)
        identity = [connection.id, connection.account_id, connection.configured_resource_id] if connection else None
    return f"{name}:{_hash(identity)[:16]}"


def _allowed(db: Session, operations: tuple[str, ...]) -> bool:
    row = db.get(AgentPolicy, "assistant")
    if row is not None:
        db.refresh(row)
    policy = AgentPolicyService(db)
    return all(policy.decision("assistant", {
        "id": operation, "category": "integration", "availability": "available", "mcp_exposed": True,
    })[0] for operation in operations)


def available_sources(db: Session, *, session_id: int) -> set[str]:
    """Revalidate identities and read authority without fetching source content."""
    try:
        AgentPolicyService(db).require_session("assistant", session_id)
    except AgentPermissionError:
        return set()
    service = build_default_integration_service(db)
    available = {"memory"}
    for name, (provider, operations) in _SOURCE_READS.items():
        key = _source_key(db, service, name, provider)
        if _allowed(db, operations) and service.provider_connected(provider):
            available.add(key)
    return available


def _metadata(item: dict, operation: str, arguments: dict, *, title: str | None = None) -> dict:
    selected = {key: item[key] for key in (
        "id", "category", "revision", "trashed", "createdAt", "updatedAt", "created_at", "updated_at",
        "last_edited_at", "done", "archived", "priority", "due_at", "start", "end", "status",
        "conversation_id", "latest_timestamp", "message_count", "unread", "has_attachment",
        "sourceId", "targetId", "kind",
    ) if key in item}
    selected["title"] = str(title if title is not None else item.get("title", item.get("summary", "")))[:300]
    selected["content_hash"] = _hash(item)
    selected["reference"] = {"operation": operation, "input": arguments}
    return selected


def _month(now: datetime, offset: int) -> datetime:
    index = now.year * 12 + now.month - 1 + offset
    return datetime(index // 12, index % 12 + 1, 1, tzinfo=UTC)


def collect_sources(db: Session, *, session_id: int | None = None) -> dict[str, dict]:
    """Read complete bounded source scopes using the Assistant's current permissions.

    Calendar covers the previous month through the next three months. Email covers
    conversations matching a date filter since the previous month's first day.
    A missing item means it left that scope, never authoritative provider deletion.
    Any failed page discards the partial snapshot, leaving the previous state intact.
    """
    service = build_default_integration_service(db)
    if session_id is None:
        state = db.get(AssistantState, 1)
        session_id = state.primary_session_id if state else None
    context = InvocationContext(principal_kind="agent", origin="backend", agent_id="assistant",
                                agent_session_id=session_id, initiating_action="assistant.source_review")
    now = datetime.now(UTC)
    after = _month(now, -1).date().isoformat()
    time_min = _month(now, -1).isoformat()
    time_max = _month(now, 4).isoformat()
    results: dict[str, dict] = {}

    def read(operation: str, arguments: dict) -> dict:
        output = service.execute_context(context, operation, arguments).output
        if not isinstance(output, dict):
            raise IntegrationError("provider_unavailable", "Invalid source response")
        if errors := output.get("provider_errors"):
            raise IntegrationError(errors[0].get("error_type", "provider_unavailable"), "Source unavailable")
        return output

    def pages(operation: str, arguments: dict, key: str, cursor_argument: str,
              cursor_key: str = "next_cursor") -> list[dict]:
        collected: list[dict] = []
        seen: set[str] = set()
        for _ in range(_MAX_PAGES):
            output = read(operation, arguments)
            items = output.get(key)
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise IntegrationError("provider_unavailable", "Invalid source items")
            identity_key = "conversation_id" if key == "conversations" else "id"
            if any(not isinstance(item.get(identity_key), str) or not item[identity_key] for item in items):
                raise IntegrationError("provider_unavailable", "Invalid source identity")
            collected.extend(items)
            if len(collected) > _MAX_ITEMS:
                raise IntegrationError("response_too_large", "Source exceeds review limit")
            cursor = output.get(cursor_key)
            if not cursor:
                if output.get("has_more"):
                    raise IntegrationError("provider_unavailable", "Incomplete source pagination")
                return collected
            if not isinstance(cursor, str) or cursor in seen:
                raise IntegrationError("provider_unavailable", "Invalid source pagination")
            seen.add(cursor)
            arguments = {**arguments, cursor_argument: cursor}
        raise IntegrationError("response_too_large", "Source exceeds page limit")

    def run(name: str, provider: str, window: str, collect) -> dict:
        source_key = _source_key(db, service, name, provider)
        scope = f"{source_key}:{window}"
        result = {"items": {}, "complete": False, "error": None, "scope": scope,
                  "removal_kind": "left_scope"}
        try:
            if session_id is None:
                raise IntegrationError("agent_permission_denied", "Assistant session unavailable")
            if not service.provider_connected(provider):
                raise IntegrationError("connection_unavailable", "Source unavailable")
            items = collect()
            if _source_key(db, service, name, provider) != source_key:
                raise IntegrationError("connection_unavailable", "Source identity changed during collection")
            if not _allowed(db, _SOURCE_READS[name][1]):
                raise IntegrationError("agent_permission_denied", "Source permission changed during collection")
            if not service.provider_connected(provider):
                raise IntegrationError("connection_unavailable", "Source became unavailable during collection")
            result["items"] = items
            result["complete"] = True
        except (IntegrationError, ValueError, TypeError, KeyError) as exc:
            result["error"] = getattr(exc, "error_type", "provider_unavailable")
        results[source_key] = result
        return result

    def atlas_records() -> dict:
        # Native revision advances with every saved record change; compact pages
        # avoid copying private data and let large individual records remain detectable.
        items = pages("atlas.records.search", {"include_data": False, "trashed": "all", "limit": 100},
                      "items", "cursor")
        return {item["id"]: _metadata(item, "atlas.records.get", {"id": item["id"]}) for item in items}

    records_snapshot = run("atlas.records", "atlas", "all-records", atlas_records)

    def atlas_links() -> dict:
        if not records_snapshot["complete"]:
            raise IntegrationError(records_snapshot["error"], "Records unavailable")
        links = {}
        for record_id, record in records_snapshot["items"].items():
            detail = read("atlas.records.get", {"id": record_id})["record"]
            if detail.get("revision") != record.get("revision"):
                raise IntegrationError("stale_revision", "Atlas changed during collection")
            for link in detail.get("links", []) + detail.get("backlinks", []):
                if not isinstance(link, dict) or not isinstance(link.get("id"), str):
                    raise IntegrationError("provider_unavailable", "Invalid Atlas link")
                links[link["id"]] = _metadata(link, "atlas.links.get", {"id": link["id"]})
                if len(links) > _MAX_ITEMS:
                    raise IntegrationError("response_too_large", "Source exceeds review limit")
        return links

    run("atlas.links", "atlas", "all-links", atlas_links)

    def todos() -> dict:
        items = pages("notion.todo.list", {"page_size": 100}, "todos", "start_cursor")
        return {item["id"]: _metadata(item, "notion.todo.list", {"page_size": 100}) for item in items}

    run("notion.todos", "notion", "configured-todos", todos)

    def calendar() -> dict:
        items = pages("google_calendar.event.list", {"time_min": time_min, "time_max": time_max, "page_size": 100},
                      "events", "page_token", "next_page_token")
        return {item["id"]: _metadata(item, "google_calendar.event.get", {"id": item["id"]}) for item in items}

    run("calendar.events", "google_calendar", f"primary:{time_min}:{time_max}", calendar)

    def email(provider: str) -> dict:
        items = pages("email.search", {"providers": [provider], "after": after, "page_size": 25},
                      "conversations", "page_token", "next_page_token")
        # Outlook groups messages within each page, so a conversation can span pages.
        grouped: dict[str, list[dict]] = {}
        for item in items:
            grouped.setdefault(item["conversation_id"], []).append(item)
        snapshots = {}
        for conversation_id, fragments in grouped.items():
            latest = dict(max(fragments, key=lambda item: (item["latest_timestamp"], _hash(item))))
            if provider == "outlook":
                latest["message_count"] = sum(item["message_count"] for item in fragments)
                latest["unread"] = any(item["unread"] for item in fragments)
                latest["has_attachment"] = any(item["has_attachment"] for item in fragments)
            snapshots[conversation_id] = _metadata(
                latest, "email.conversation.get", {"provider": provider, "conversation_id": conversation_id},
                title=latest.get("subject", ""),
            )
        return snapshots

    for provider in ("gmail", "outlook"):
        run(f"email.{provider}", provider, f"conversation-summaries:after={after}", lambda p=provider: email(p))
    try:
        if session_id is None:
            raise AgentPermissionError("Assistant session unavailable")
        AgentPolicyService(db).require_session("assistant", session_id)
        memory_available = True
    except AgentPermissionError:
        memory_available = False
    from app.services.agent_memory_service import AgentMemoryService

    results["memory"] = {"items": {
        str(fact["id"]): {"id": fact["id"], "key": fact["key"], "category": "facts",
                       "content_hash": _hash({"value": fact["value"], "provenance": fact.get("provenance_json")}),
                       "reference": {"memory_id": fact["id"], "path": f"memory/facts-outside-atlas/{fact['id']}.json"}}
        for fact in AgentMemoryService(db).list_facts()
        if _unexpired(fact.get("expires_at"), now)
    } if memory_available else {}, "complete": memory_available,
        "error": None if memory_available else "agent_permission_denied",
        "scope": "current-unexpired-memory", "removal_kind": "left_scope"}
    return results
