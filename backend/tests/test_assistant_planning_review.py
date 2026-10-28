from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import ActSession, ActTurn, AssistantState
from app.services.assistant_planning_review import (
    planning_review_error,
    planning_review_status,
    record_planning_read,
)

NOW = datetime(2026, 10, 25, 14, tzinfo=UTC)
OPERATIONS = ("notion.todo.list", "atlas.goal.list", "atlas.records.search")


@pytest.fixture
def db(tmp_path, monkeypatch):
    from app.services.function_catalog_service import FunctionCatalogService

    monkeypatch.setattr(FunctionCatalogService, "list_entries", lambda *_args, **_kwargs: [
        {"id": operation, "category": "integration", "availability": "available", "mcp_exposed": True,
         "risk_level": "low", "mcp_read_only": True} for operation in OPERATIONS])
    engine = create_engine(f"sqlite:///{tmp_path / 'planning-review.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(AssistantState(id=1, timezone="America/Toronto", enabled=True,
                                   planning_contract_migrated=True, agenda_schedules_migrated=True))
        session.add(ActSession(id=1, agent_id="assistant", codex_thread_id="review", status="active"))
        session.flush()
        session.add(ActTurn(id=1, session_id=1, status="running", user_message="Plan today", created_at=NOW,
                            assistant_review_json={"prepared_at": "source tracking timestamp"},
                            assistant_context_json={"kind": "agenda", "completion_requirement": "morning_plan",
                                                    "occurrence": {"id": 10, "timezone": "America/Toronto"}}))
        session.commit()
        yield session
    engine.dispose()


def read(db, operation, arguments=None, output=None, **kwargs):
    record_planning_read(db, 1, session_id=1, operation=operation, arguments=arguments or {},
                         output=output, now=kwargs.pop("now", NOW), **kwargs)


def status(db, now=NOW):
    return planning_review_status(db, db.get(ActTurn, 1), now)


def todos(db, *, cursor=None, next_cursor=None, items=None, now=NOW):
    read(db, "notion.todo.list", {"page_size": 100, **({"start_cursor": cursor} if cursor else {})},
         {"todos": items or [], "has_more": next_cursor is not None, "next_cursor": next_cursor}, now=now)


def goals(db, *, items=None, arguments=None):
    read(db, "atlas.goal.list", arguments or {"limit": 100}, {"goals": items or [], "graph": {}})


def test_morning_requires_actual_reads_even_without_detected_changes(db):
    turn = db.get(ActTurn, 1)
    assert "todos and goals" in planning_review_error(db, turn, NOW)
    todos(db)
    assert "goals" in planning_review_error(db, turn, NOW)
    goals(db)
    assert planning_review_error(db, turn, NOW) is None
    assert status(db)["satisfied"]
    assert status(db)["unavailable"] == []
    assert all(source["status"] == "reviewed" for source in status(db)["sources"].values())


def test_contiguous_todo_pages_and_metadata_only(db):
    todos(db, next_cursor="page-2", items=[{"title": "PRIVATE TODO BODY", "notes": "secret note"}])
    assert status(db)["sources"]["todos"]["status"] == "incomplete"
    todos(db, cursor="page-2", next_cursor="page-3", items=[{"title": "second page"}])
    todos(db, cursor="page-3", items=[{"title": "third page"}])
    receipt = status(db)["sources"]["todos"]
    assert receipt["status"] == "reviewed" and receipt["pages"] == 3 and receipt["count"] == 3
    review = db.get(ActTurn, 1).assistant_review_json
    assert review["prepared_at"] == "source tracking timestamp"
    assert "PRIVATE TODO BODY" not in str(review) and "secret note" not in str(review)


@pytest.mark.parametrize("cursor", ["arbitrary", "page-3"])
def test_unread_pages_cannot_be_skipped(db, cursor):
    todos(db, next_cursor="page-2")
    todos(db, cursor=cursor)
    assert status(db)["sources"]["todos"]["reason"] == "missing_previous_page"
    assert not status(db)["satisfied"]


def test_new_first_page_resets_coverage_and_valid_chain_can_restart(db):
    todos(db)
    assert status(db)["sources"]["todos"]["status"] == "reviewed"
    todos(db, next_cursor="new-page")
    assert status(db)["sources"]["todos"]["status"] == "incomplete"
    todos(db, cursor="new-page")
    assert status(db)["sources"]["todos"]["status"] == "reviewed"


@pytest.mark.parametrize("output", [
    {"todos": [], "has_more": True, "next_cursor": None},
    {"todos": [], "has_more": False, "next_cursor": "cursor"},
    {"todos": [], "has_more": "false", "next_cursor": None},
    {"todos": [], "has_more": False, "next_cursor": ""},
    {"todos": None, "has_more": False, "next_cursor": None},
])
def test_invalid_todo_pagination_stays_incomplete(db, output):
    read(db, "notion.todo.list", {}, output)
    assert status(db)["sources"]["todos"]["status"] == "incomplete"


@pytest.mark.parametrize("arguments", [{}, {"limit": 25}, {"limit": 100, "pursuit_status": "active"},
                                       {"limit": 100, "horizon": "long"}])
def test_small_or_filtered_goal_reads_do_not_cover_all_goals(db, arguments):
    read(db, "atlas.goal.list", arguments, {"goals": [], "graph": {}})
    assert status(db)["sources"]["goals"]["status"] == "incomplete"


def test_saturated_goals_need_the_complete_paginated_record_fallback(db):
    goals(db, items=[{"title": "private goal"}] * 100)
    assert status(db)["sources"]["goals"]["reason"] == "use_paginated_goal_records"
    arguments = {"category": "goal", "include_data": True, "trashed": "false", "limit": 100}
    read(db, "atlas.records.search", arguments, {"items": [{}] * 100, "next_cursor": "goal-2", "total": 101})
    read(db, "atlas.records.search", {**arguments, "cursor": "goal-2"},
         {"items": [{}], "next_cursor": None, "total": 101})
    receipt = status(db)["sources"]["goals"]
    assert receipt["status"] == "reviewed" and receipt["count"] == 101
    assert "private goal" not in str(db.get(ActTurn, 1).assistant_review_json)


@pytest.mark.parametrize("arguments", [
    {"category": "goal"},
    {"category": "goal", "include_data": False}, {"category": "goal", "include_data": True, "query": "active"},
    {"category": "goal", "include_data": True, "trashed": "true"},
])
def test_generic_goal_search_requires_complete_unfiltered_goal_data(db, arguments):
    read(db, "atlas.records.search", arguments, {"items": [], "next_cursor": None, "total": 0})
    assert status(db)["sources"]["goals"]["status"] == "incomplete"


def test_filtered_followup_does_not_erase_a_prior_full_review(db):
    goals(db)
    goals(db, arguments={"pursuit_status": "active", "limit": 100})
    assert not db.in_transaction()  # The shared state write lock must be released.
    assert status(db)["sources"]["goals"]["status"] == "reviewed"


@pytest.mark.parametrize("category", ["interest", "person", None])
def test_unrelated_atlas_reads_do_not_claim_or_revoke_goal_coverage(db, category):
    arguments = {"include_data": True, **({"category": category} if category else {})}
    read(db, "atlas.records.search", arguments, {"items": [], "next_cursor": None, "total": 0})
    assert status(db)["sources"]["goals"]["status"] == "missing"
    goals(db)
    read(db, "atlas.records.search", arguments, {"items": [], "next_cursor": None, "total": 0})
    read(db, "atlas.records.search", {"category": "goal", "include_data": True, "query": "one goal"},
         {"items": [], "next_cursor": None, "total": 0})
    assert status(db)["sources"]["goals"]["status"] == "reviewed"


@pytest.mark.parametrize("error_type,expected", [("connection_unavailable", "unavailable"),
    ("atlas_locked", "unavailable"), ("agent_permission_denied", "unavailable"),
    ("provider_timeout", "unavailable"), ("invalid_input", "incomplete"), ("not_found", "incomplete")])
def test_real_unavailability_is_distinct_from_success_and_invalid_requests(db, error_type, expected):
    read(db, "notion.todo.list", {}, error_type=error_type)
    goals(db)
    assert status(db)["sources"]["todos"]["status"] == expected
    assert status(db)["satisfied"] is (expected == "unavailable")
    assert "count" not in status(db)["sources"]["todos"]


@pytest.mark.parametrize("change", ["foreign_session", "other_agent", "archived_session", "completed_turn",
                                    "cancelled", "ordinary_turn"])
def test_other_or_inactive_turns_cannot_record_morning_coverage(db, change):
    turn, session = db.get(ActTurn, 1), db.get(ActSession, 1)
    if change == "foreign_session":
        turn.session_id = 2
    elif change == "other_agent":
        session.agent_id = "act"
    elif change == "archived_session":
        session.status = "archived"
    elif change == "completed_turn":
        turn.status = "succeeded"
    elif change == "cancelled":
        turn.cancel_requested_at = NOW
    else:
        turn.assistant_context_json = None
    db.commit()
    todos(db)
    assert "planning_sources" not in turn.assistant_review_json


def test_previous_day_timezone_and_other_attempt_receipts_are_not_reused(db):
    todos(db)
    goals(db)
    assert not status(db, NOW + timedelta(days=1))["satisfied"]
    db.get(AssistantState, 1).timezone = "Asia/Tokyo"
    db.commit()
    assert not status(db)["satisfied"]
    db.get(AssistantState, 1).timezone = "America/Toronto"
    turn = db.get(ActTurn, 1)
    turn.assistant_review_json = {**turn.assistant_review_json, "planning_sources": {
        **turn.assistant_review_json["planning_sources"], "turn_id": 2}}
    db.commit()
    assert not status(db)["satisfied"]


def test_missing_actual_capability_is_an_explicit_blocker_not_a_fake_review(db, monkeypatch):
    from app.services.function_catalog_service import FunctionCatalogService

    monkeypatch.setattr(FunctionCatalogService, "list_entries", lambda *_: [])
    result = status(db)
    assert result["satisfied"] and result["unavailable"] == ["todos", "goals"]
    assert result["sources"]["todos"]["error_type"] == "function_unavailable"
    assert "planning_sources" not in db.get(ActTurn, 1).assistant_review_json


def test_banned_sources_are_blockers_but_available_fallback_must_be_read(db, monkeypatch):
    from app.services.agent_policy_service import AgentPolicyService

    monkeypatch.setattr(AgentPolicyService, "decision", lambda self, agent, entry:
                        (entry["id"] == "atlas.records.search", "Test policy"))
    result = status(db)
    assert result["sources"]["todos"]["status"] == "unavailable"
    assert result["sources"]["goals"]["status"] == "missing"
    assert not result["satisfied"]


def test_full_reads_do_not_load_catalog_and_internal_errors_are_not_silenced(db, monkeypatch):
    from app.services.function_catalog_service import FunctionCatalogService

    def unexpected(*_args):
        raise RuntimeError("catalog bug")

    todos(db)
    goals(db)
    monkeypatch.setattr(FunctionCatalogService, "list_entries", unexpected)
    assert status(db)["satisfied"]
    with pytest.raises(RuntimeError, match="catalog bug"):
        status(db, NOW + timedelta(days=1))
