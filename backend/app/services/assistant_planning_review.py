"""Small, server-recorded receipts for mandatory morning source reads.

Receipts describe coverage, never cache source records or claim model analysis.
Only the authenticated MCP invocation path supplies outcomes to this module.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import update

from app.models import ActSession, ActTurn, AssistantState

_SOURCES = {"notion.todo.list": "todos", "atlas.goal.list": "goals", "atlas.records.search": "goals"}
_UNAVAILABLE = frozenset({
    "connection_unavailable", "atlas_locked", "authorization_missing_or_stale", "agent_permission_denied",
    "function_unavailable", "function_banned", "provider_forbidden", "invalid_credential", "invalid_credentials",
    "provider_unavailable", "provider_timeout", "rate_limit", "rate_limited", "internal_failure",
    "response_too_large", "operation_undeclared", "not_exposed", "stale_contract",
})


def _scope(db, turn, now):
    activation = turn.assistant_context_json or {}
    occurrence = activation.get("occurrence")
    if activation.get("completion_requirement") != "morning_plan" or not isinstance(occurrence, dict):
        return None
    state = db.get(AssistantState, 1, populate_existing=True)
    timezone = state.timezone if state else occurrence.get("timezone")
    if not timezone:
        return None
    day = now.replace(tzinfo=now.tzinfo or UTC).astimezone(ZoneInfo(timezone)).date().isoformat()
    return {"turn_id": turn.id, "occurrence_id": occurrence.get("id"), "day": day, "timezone": timezone}


def _same_scope(receipt, scope):
    return isinstance(receipt, dict) and all(receipt.get(key) == value for key, value in scope.items())


def _filtered(operation, arguments):
    if operation == "notion.todo.list":
        return bool(set(arguments) - {"page_size", "start_cursor"})
    if operation == "atlas.goal.list":
        return bool(set(arguments) - {"limit"})
    return (arguments.get("category") != "goal" or arguments.get("include_data") is not True
            or arguments.get("trashed", "false") != "false" or bool(arguments.get("query"))
            or bool(set(arguments) - {"category", "include_data", "trashed", "query", "limit", "cursor"}))


def _page(previous, operation, arguments, output):
    """Verify a contiguous bounded cursor chain, retaining only its next cursor."""
    todo = operation == "notion.todo.list"
    cursor = arguments.get("start_cursor" if todo else "cursor")
    items = output.get("todos" if todo else "items")
    next_cursor = output.get("next_cursor")
    if (not isinstance(items, list) or len(items) > 100
            or next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor or len(next_cursor) > 2048)):
        return {"status": "incomplete", "reason": "invalid_page"}
    if cursor is None:
        count, pages = 0, 0
    elif (not isinstance(previous, dict) or previous.get("operation") != operation
          or previous.get("status") != "incomplete" or previous.get("next_cursor") != cursor):
        return {"status": "incomplete", "reason": "missing_previous_page"}
    else:
        count, pages = previous["count"], previous["pages"]
    count += len(items)
    pages += 1
    if pages > 100 or count > 10000:
        return {"status": "incomplete", "reason": "coverage_limit"}
    if next_cursor == cursor and next_cursor is not None:
        return {"status": "incomplete", "reason": "repeated_cursor"}
    if todo:
        more = output.get("has_more")
        if type(more) is not bool or more != (next_cursor is not None):
            return {"status": "incomplete", "reason": "invalid_pagination"}
    else:
        total = output.get("total")
        if type(total) is not int or total < count or next_cursor is None and total != count:
            return {"status": "incomplete", "reason": "incomplete_total"}
        if cursor is not None and previous.get("total") != total:
            return {"status": "incomplete", "reason": "changed_total"}
    result = {"status": "incomplete" if next_cursor else "reviewed", "operation": operation,
              "count": count, "pages": pages, "next_cursor": next_cursor}
    if not todo:
        result["total"] = output["total"]
    return result


def record_planning_read(db, turn_id, *, session_id, operation, arguments, output=None,
                         error_type=None, now=None):
    """Observe a current authenticated morning read after its bounded MCP result."""
    if operation not in _SOURCES or turn_id is None or not isinstance(arguments, dict):
        return
    if operation == "atlas.records.search" and arguments.get("category") != "goal":
        return
    turn = db.get(ActTurn, turn_id, populate_existing=True)
    session = db.get(ActSession, session_id, populate_existing=True)
    if (turn is None or session is None or session.agent_id != "assistant" or session.status != "active"
            or turn.session_id != session_id or turn.status != "running" or turn.cancel_requested_at is not None):
        return
    now = now or datetime.now(UTC)
    scope = _scope(db, turn, now)
    if scope is None:
        return
    db.execute(update(AssistantState).where(AssistantState.id == 1).values(id=1))
    db.refresh(turn)
    db.refresh(session)
    scope = _scope(db, turn, now)
    if (turn.status != "running" or turn.cancel_requested_at is not None or scope is None
            or turn.session_id != session_id or session.agent_id != "assistant" or session.status != "active"):
        db.commit()
        return
    review = turn.assistant_review_json or {}
    current = review.get("planning_sources")
    sources = dict(current.get("sources", {})) if _same_scope(current, scope) else {}
    key = _SOURCES[operation]
    previous = sources.get(key)
    if _filtered(operation, arguments):
        # Focused reads remain useful after the mandatory complete review.
        if previous and previous.get("status") == "reviewed":
            db.commit()
            return
        result = {"status": "incomplete", "reason": "filtered_read"}
    elif error_type is not None:
        normalized = error_type if isinstance(error_type, str) and len(error_type) <= 64 else "internal_failure"
        result = {"status": "unavailable" if normalized in _UNAVAILABLE else "incomplete",
                  "error_type": normalized}
    elif not isinstance(output, dict):
        result = {"status": "incomplete", "reason": "invalid_output"}
    elif operation == "atlas.goal.list":
        goals, limit = output.get("goals"), arguments.get("limit", 25)
        if (type(limit) is not int or limit != 100 or not isinstance(goals, list)
                or len(goals) > limit):
            result = {"status": "incomplete", "reason": "use_unfiltered_limit_100"}
        else:
            result = {"status": "reviewed" if len(goals) < limit else "incomplete",
                      "operation": operation, "count": len(goals), "pages": 1}
            if len(goals) == limit:
                result["reason"] = "use_paginated_goal_records"
    else:
        result = _page(previous, operation, arguments, output)
    result["checked_at"] = now.replace(tzinfo=now.tzinfo or UTC).astimezone(UTC).isoformat()
    sources[key] = result
    turn.assistant_review_json = {**review, "planning_sources": {**scope, "sources": sources}}
    db.commit()


def planning_review_status(db, turn, now):
    """Expose coverage or real unavailability for this attempt and current day."""
    scope = _scope(db, turn, now)
    receipt = (turn.assistant_review_json or {}).get("planning_sources")
    sources = receipt.get("sources", {}) if scope and _same_scope(receipt, scope) else {}
    result = {**(scope or {}), "sources": {name: dict(sources.get(name, {"status": "missing"}))
                                          for name in ("todos", "goals")}}
    missing = [name for name, source in result["sources"].items() if source["status"] == "missing"]
    if scope and missing:
        from app.services.agent_policy_service import AgentPolicyService
        from app.services.function_catalog_service import FunctionCatalogService

        entries = {entry["id"]: entry for entry in FunctionCatalogService(db).list_entries()}
        candidates = {"todos": ("notion.todo.list",), "goals": ("atlas.goal.list", "atlas.records.search")}
        policy = AgentPolicyService(db)
        for source in missing:
            available = [entries[operation] for operation in candidates[source] if operation in entries
                         and entries[operation].get("availability") == "available"
                         and entries[operation].get("mcp_exposed") is True]
            if not available or not any(policy.decision("assistant", entry)[0] for entry in available):
                result["sources"][source] = {"status": "unavailable", "reason": "current_capability",
                                             "error_type": "agent_permission_denied" if available
                                             else "function_unavailable"}
    result["satisfied"] = bool(scope) and all(source["status"] in {"reviewed", "unavailable"}
                                               for source in result["sources"].values())
    result["unavailable"] = [name for name, source in result["sources"].items()
                             if source["status"] == "unavailable"]
    return result


def planning_review_error(db, turn, now):
    """Require actual source coverage before a morning occurrence can succeed."""
    if _scope(db, turn, now) is None:
        return None
    status = planning_review_status(db, turn, now)
    missing = [name for name, source in status["sources"].items()
               if source["status"] not in {"reviewed", "unavailable"}]
    if missing:
        return ("Morning plan is incomplete: review all current " + " and ".join(missing)
                + " through their source tools, following every page even when nothing changed.")
    return None
