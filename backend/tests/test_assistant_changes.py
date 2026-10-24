from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import ActSession, ActTurn, AssistantChange, AssistantSourceState
from app.services.assistant_activation_service import AssistantActivationService
from app.services.assistant_changes import AssistantChangeService
from app.services.assistant_service import AssistantService
from app.services.assistant_turn_dispatcher import AssistantTurnDispatcher


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'changes.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr("app.services.assistant_sources.collect_sources", lambda *_a, **_k: {})
    monkeypatch.setattr("app.services.assistant_sources.available_sources",
                        lambda db, **_k: set(db.scalars(select(AssistantSourceState.source))))
    with Session(engine) as session:
        AssistantService(session).state()
        yield session
    engine.dispose()


def snapshot(items, *, scope="all", complete=True, error=None):
    return {"items": items, "scope": scope, "complete": complete, "error": error}


def new_turn(db, *, background=True):
    session = ActSession(agent_id="assistant", codex_thread_id=f"pending:{uuid4()}")
    db.add(session)
    db.flush()
    turn = ActTurn(session_id=session.id, user_message="Review", status="running",
                   assistant_context_json={"kind": "daily"} if background else None)
    db.add(turn)
    db.commit()
    return turn


def prepared_turn(db, sources=("calendar",), *, background=True):
    turn = new_turn(db, background=background)
    turn.assistant_review_json = {"sources": list(sources), "unavailable_sources": {}}
    db.commit()
    return turn


def complete(db, turn, status="succeeded"):
    turn.status = status
    turn.completed_at = datetime.now(UTC)
    AssistantChangeService(db).finish(turn)
    db.commit()


def test_change_review_preserves_independent_initialization_receipt(db):
    turn = new_turn(db)
    marker = {"batch": 3, "summary_saved": True}
    turn.assistant_review_json = {"initialization": marker}
    db.commit()
    service = AssistantChangeService(db)
    assert service.read(turn.id)["total"] == 0
    assert turn.assistant_review_json["initialization"] == marker
    assert "sources" in turn.assistant_review_json


def test_backend_detects_baseline_updates_additions_and_disappearance_without_duplicates(db):
    service = AssistantChangeService(db)
    first = {"a": {"title": "Meeting", "revision": 1}, "b": {"title": "Other"}}
    service.record_snapshots({"calendar": snapshot(first)})
    service.record_snapshots({"calendar": snapshot(first)})
    assert [c.kind for c in db.scalars(select(AssistantChange))] == ["baseline", "baseline"]
    service.record_snapshots({"calendar": snapshot({"a": {"title": "Meeting", "revision": 2},
                                                   "c": {"title": "New"}})})
    rows = list(db.scalars(select(AssistantChange).order_by(AssistantChange.id)))
    assert [row.kind for row in rows] == ["baseline", "baseline", "updated", "added", "removed_from_scope"]
    assert rows[2].before_json["revision"] == 1 and rows[2].after_json["revision"] == 2
    assert rows[-1].after_json is None
    # A process/session restart reuses the persisted baseline, with no replay.
    with Session(db.bind) as restarted:
        AssistantChangeService(restarted).record_snapshots({"calendar": snapshot({
            "a": {"title": "Meeting", "revision": 2}, "c": {"title": "New"},
        })})
    assert len(list(db.scalars(select(AssistantChange)))) == 5


def test_failed_partial_source_retains_baseline_while_other_sources_advance(db):
    service = AssistantChangeService(db)
    service.record_snapshots({"calendar": snapshot({"a": {"revision": 1}}), "mail": snapshot({})})
    old_success = db.get(AssistantSourceState, "calendar").succeeded_at
    service.record_snapshots({"calendar": snapshot({}, complete=False, error="provider_timeout"),
                              "mail": snapshot({"m": {"title": "Hello"}})})
    state = db.get(AssistantSourceState, "calendar")
    assert state.snapshot_json == {"a": {"revision": 1}}
    assert state.succeeded_at == old_success and state.error == "provider_timeout"
    assert service.status()["pending_count"] == 2
    service.record_snapshots({"calendar": snapshot({"a": {"revision": 1}})})
    assert db.get(AssistantSourceState, "calendar").error is None
    assert service.status()["pending_count"] == 2


def test_window_scope_shift_never_reports_old_items_as_deleted(db):
    service = AssistantChangeService(db)
    service.record_snapshots({"calendar": snapshot({"old": {}, "shared": {"revision": 1}}, scope="oct")})
    service.record_snapshots({"calendar": snapshot({"shared": {"revision": 2}, "new": {}}, scope="nov")})
    rows = list(db.scalars(select(AssistantChange).order_by(AssistantChange.id)))
    assert [row.kind for row in rows] == ["baseline", "baseline", "updated", "entered_scope"]


@pytest.mark.parametrize("terminal", ["failed", "cancelled", "interrupted"])
def test_failed_review_retries_same_changes_and_only_success_acknowledges(db, terminal):
    service = AssistantChangeService(db)
    service.record_snapshots({"calendar": snapshot({"one": {"title": "Prepare"}})})
    first = prepared_turn(db)
    page = service.read(first.id)
    assert page["total"] == 1
    complete(db, first, terminal)
    assert service.status()["pending_count"] == 1
    second = prepared_turn(db)
    assert service.read(second.id)["items"][0]["id"] == page["items"][0]["id"]
    complete(db, second)
    assert service.status()["pending_count"] == 0
    assert service.read(prepared_turn(db).id)["items"] == []


def test_pagination_acknowledges_only_delivered_items_and_new_versions_stay_pending(db):
    service = AssistantChangeService(db)
    service.record_snapshots({"calendar": snapshot({str(i): {"revision": 1} for i in range(5)})})
    turn = prepared_turn(db)
    page = service.read(turn.id, limit=2)
    assert page["total"] == 5 and page["next_offset"] == 2
    # Rebuilding a prompt/page is idempotent and doesn't consume extra work.
    assert service.read(turn.id, limit=2)["items"] == page["items"]
    service.record_snapshots({"calendar": snapshot({str(i): {"revision": 2 if i == 0 else 1} for i in range(5)})})
    complete(db, turn)
    assert service.status()["pending_count"] == 4
    next_turn = prepared_turn(db)
    remaining = service.read(next_turn.id)
    assert len(remaining["items"]) == 4 and remaining["items"][-1]["kind"] == "updated"


def test_paginated_pages_are_all_receipted_and_running_reviews_have_exclusive_claims(db):
    service = AssistantChangeService(db)
    service.record_snapshots({"calendar": snapshot({str(i): {} for i in range(4)})})
    first = prepared_turn(db)
    second = prepared_turn(db)
    assert len(service.read(first.id, limit=2)["items"]) == 2
    assert len(service.read(second.id, limit=1)["items"]) == 1
    # First review sees its own two entries plus the still unclaimed last one.
    page = service.read(first.id, offset=2, limit=2)
    assert len(page["items"]) == 1 and page["next_offset"] is None
    complete(db, first)
    assert service.status()["pending_count"] == 1
    complete(db, second)
    assert service.status()["pending_count"] == 0


def test_recurring_task_explicit_review_collects_once_and_acknowledges_only_on_success(db, monkeypatch):
    calls = []

    def collect(*_a, **kwargs):
        calls.append(kwargs["session_id"])
        return {"calendar": snapshot({"event": {"title": "Moved", "revision": 3}}),
                "mail": snapshot({}, complete=False, error="connection_unavailable")}

    monkeypatch.setattr("app.services.assistant_sources.collect_sources", collect)
    turn = new_turn(db)
    prompt = AssistantTurnDispatcher._turn_input(db, turn, recovered_thread=False)
    assert calls == [] and "Moved" not in prompt
    result = AssistantChangeService(db).read(turn.id)
    assert result["items"][0]["kind"] == "baseline"
    assert "Moved" in str(result)
    assert "connection_unavailable" in str(result)
    AssistantTurnDispatcher._turn_input(db, turn, recovered_thread=True)
    assert calls == [turn.session_id]
    assert AssistantChangeService(db).status()["pending_count"] == 1
    complete(db, turn)
    assert AssistantChangeService(db).status()["pending_count"] == 0


def test_normal_conversation_stays_light_and_explicit_change_read_collects_once(db, monkeypatch):
    calls = []

    def collect(*_a, **_kw):
        calls.append(True)
        return {"calendar": snapshot({"event": {}})}

    monkeypatch.setattr("app.services.assistant_sources.collect_sources", collect)
    turn = new_turn(db, background=False)
    AssistantTurnDispatcher._turn_input(db, turn, recovered_thread=False)
    assert calls == [] and turn.assistant_context_json is None
    service = AssistantChangeService(db)
    assert service.read(turn.id)["total"] == 1
    service.read(turn.id)
    assert calls == [True] and turn.assistant_context_json is None


def test_new_account_and_failed_sources_do_not_expose_cached_pending_changes(db):
    service = AssistantChangeService(db)
    service.record_snapshots({"old-account": snapshot({"private": {"title": "Old subject"}}),
                              "failed": snapshot({"cached": {}})})
    turn = prepared_turn(db, sources=("new-account",))
    assert service.read(turn.id)["items"] == []
    complete(db, turn)
    assert service.status()["pending_count"] == 2


def test_permission_or_identity_change_between_pages_blocks_remaining_changes(db, monkeypatch):
    service = AssistantChangeService(db)
    service.record_snapshots({"calendar": snapshot({"a": {}, "b": {}})})
    turn = prepared_turn(db)
    assert service.read(turn.id, limit=1)["next_offset"] == 1
    monkeypatch.setattr("app.services.assistant_sources.available_sources", lambda *_a, **_k: set())
    assert service.read(turn.id, offset=1)["items"] == []
    complete(db, turn)
    assert service.status()["pending_count"] == 1


def test_success_reconciliation_is_idempotent_after_restart(db):
    service = AssistantChangeService(db)
    service.record_snapshots({"calendar": snapshot({"event": {}})})
    turn = prepared_turn(db)
    service.read(turn.id)
    turn.status = "succeeded"
    turn.completed_at = datetime.now(UTC)
    db.commit()  # Simulate terminal status persisted through an older completion path.
    AssistantActivationService(db).on_turn_completed(turn)
    assert service.status()["pending_count"] == 0
    service.reconcile()
    db.commit()
    assert service.status()["pending_count"] == 0
