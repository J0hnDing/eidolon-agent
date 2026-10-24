import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import ActSession, ActTurn, AssistantState
from app.services.assistant_initialization import AssistantInitializationService
from app.services.assistant_service import AssistantService
from app.services.integration_service import IntegrationError


def task(name):
    return {"operation": name, "input": {}}


class Atlas:
    def __init__(self):
        self.identity = "atlas-one"
        self.connected = True
        self.tasks = [task("root")]
        self.calls = []
        self.handlers = {}

    def read(self, db, reference, *, session_id, turn_id, at):
        self.calls.append(reference)
        assert session_id and turn_id and datetime.fromisoformat(at).tzinfo is not None
        result = self.handlers.get(reference["operation"])
        return result(reference) if result else {"data": {"value": reference["operation"]},
                                                 "next_tasks": [], "continuation": None}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.act_workspace_service.ACT_ROOT", tmp_path / "agents")
    engine = create_engine(f"sqlite:///{tmp_path / 'initialization.db'}")
    Base.metadata.create_all(engine)
    atlas = Atlas()
    monkeypatch.setattr("app.services.assistant_initialization._identity", lambda: atlas.identity)
    monkeypatch.setattr("app.services.assistant_initialization.initial_tasks", lambda at: list(atlas.tasks))
    monkeypatch.setattr("app.services.assistant_initialization.read_task", atlas.read)
    monkeypatch.setattr("app.services.assistant_initialization.build_default_integration_service",
                        lambda db: SimpleNamespace(provider_connected=lambda provider: atlas.connected))
    with Session(engine) as db:
        AssistantService(db).state()
        yield db, atlas
    engine.dispose()


def new_turn(db, *, context=True):
    session = ActSession(agent_id="assistant", codex_thread_id=f"pending:{uuid4()}")
    db.add(session)
    db.flush()
    turn = ActTurn(session_id=session.id, user_message="Initialize Atlas", status="running",
                   assistant_context_json={"initialize_atlas": True} if context else None)
    db.add(turn)
    db.commit()
    return turn


def read(db, turn):
    return AssistantInitializationService(db).read(turn.session_id, turn.id)


def acknowledge(db, turn):
    return AssistantInitializationService(db).acknowledge(turn.id)


def finish(db, turn, status="succeeded"):
    turn.status = status
    turn.completed_at = datetime.now(UTC)
    AssistantInitializationService(db).finish(turn)
    db.commit()


def progress(db):
    db.expire_all()
    return db.get(AssistantState, 1).initialization_json


def test_read_discovers_all_children_deduplicates_links_and_keeps_synthesis_separate(setup):
    db, atlas = setup
    atlas.tasks = [task("record-a"), task("record-b")]
    atlas.handlers = {name: lambda ref: {"data": {"record": ref["operation"]},
                                        "next_tasks": [task("shared-link")], "continuation": None}
                      for name in ("record-a", "record-b")}
    turn = new_turn(db)
    result = read(db, turn)
    assert [item["reference"]["operation"] for item in result["items"]] == [
        "record-a", "record-b", "shared-link",
    ]
    assert result["phase"] == "reading"
    assert [ref["operation"] for ref in atlas.calls] == ["record-a", "record-b", "shared-link"]
    assert len(progress(db)["pending"]) == 3  # Reading alone cannot advance receipts.
    acknowledge(db, turn)
    assert progress(db)["batches"] == 0
    finish(db, turn)
    assert progress(db)["pending"] == [{"operation": "assistant.synthesize", "input": {}}]
    synthesis = new_turn(db)
    output = read(db, synthesis)
    assert output["phase"] == "synthesis" and len(output["items"]) == 1
    assert output["items"][0]["data"] == {"phase": "synthesis"}
    assert AssistantInitializationService(db).status()["status"] != "complete"
    acknowledge(db, synthesis)
    finish(db, synthesis)
    assert AssistantInitializationService(db).status()["status"] == "complete"
    assert progress(db)["batches"] == 2
    assert not AssistantInitializationService(db).ready(datetime.now(UTC))


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted", "running"])
def test_failed_cancelled_or_interrupted_turn_never_advances_read_batch(setup, status):
    db, _atlas = setup
    turn = new_turn(db)
    read(db, turn)
    pending = progress(db)["pending"]
    acknowledge(db, turn)
    finish(db, turn, status)
    assert progress(db)["pending"] == pending
    assert progress(db)["batches"] == 0
    assert AssistantInitializationService(db).status()["status"] == "blocked"


def test_cancellation_after_acknowledgement_never_consumes_delivered_batch(setup):
    db, _atlas = setup
    turn = new_turn(db)
    read(db, turn)
    acknowledge(db, turn)
    pending = progress(db)["pending"]
    turn.cancel_requested_at = datetime.now(UTC)
    finish(db, turn)
    assert progress(db)["pending"] == pending and progress(db)["batches"] == 0


@pytest.mark.parametrize("missing", ["read", "acknowledgement"])
def test_success_requires_delivered_batch_and_acknowledgement(setup, missing):
    db, _atlas = setup
    turn = new_turn(db)
    if missing == "read":
        # Start durable work in another turn, then an unrelated success cannot consume it.
        original = new_turn(db)
        read(db, original)
        with pytest.raises(ValueError, match="Read the current"):
            acknowledge(db, turn)
    else:
        read(db, turn)
    pending = progress(db)["pending"]
    finish(db, turn)
    assert progress(db)["pending"] == pending and progress(db)["batches"] == 0


def test_acknowledgement_requires_delivery_and_only_success_consumes_batch(setup):
    db, _atlas = setup
    turn = new_turn(db)
    with pytest.raises(ValueError, match="Read the current"):
        acknowledge(db, turn)
    assert progress(db) == {}
    read(db, turn)
    before = progress(db)
    result = acknowledge(db, turn)
    assert result["acknowledged"] and result["batch"] == 0
    assert progress(db) == before
    assert db.get(ActTurn, turn.id).assistant_review_json["initialization"]["reviewed"] is True
    assert acknowledge(db, turn) == result
    finish(db, turn)
    assert progress(db)["batches"] == 1


@pytest.mark.parametrize("invalid", ["agent", "session", "cancelled", "terminal", "missing"])
def test_acknowledgement_rejects_non_running_assistant_turns(setup, invalid):
    db, _atlas = setup
    turn = new_turn(db)
    read(db, turn)
    session = db.get(ActSession, turn.session_id)
    if invalid == "agent":
        session.agent_id = "act"
    elif invalid == "session":
        session.status = "archived"
    elif invalid == "cancelled":
        turn.cancel_requested_at = datetime.now(UTC)
    elif invalid == "terminal":
        turn.status = "succeeded"
    db.commit()
    before = progress(db)
    with pytest.raises(ValueError, match="current running"):
        AssistantInitializationService(db).acknowledge(turn.id + 100 if invalid == "missing" else turn.id)
    assert progress(db) == before
    assert not db.get(ActTurn, turn.id).assistant_review_json["initialization"]["reviewed"]


def test_acknowledgement_rejects_changed_atlas_and_old_batch_receipts(setup):
    db, atlas = setup
    old = new_turn(db)
    read(db, old)
    acknowledge(db, old)
    finish(db, old)
    old.status = "running"
    db.commit()
    with pytest.raises(ValueError, match="Read the current"):
        acknowledge(db, old)
    current = new_turn(db)
    read(db, current)
    atlas.identity = "atlas-two"
    with pytest.raises(ValueError, match="Read the current"):
        acknowledge(db, current)
    assert progress(db)["batches"] == 1


def test_reread_retains_review_only_for_identical_delivered_content(setup):
    db, atlas = setup
    turn = new_turn(db)
    read(db, turn)
    acknowledge(db, turn)
    read(db, turn)
    assert db.get(ActTurn, turn.id).assistant_review_json["initialization"]["reviewed"]
    atlas.handlers["root"] = lambda ref: {"data": {"value": "changed"},
                                           "next_tasks": [], "continuation": None}
    read(db, turn)
    assert not db.get(ActTurn, turn.id).assistant_review_json["initialization"]["reviewed"]
    finish(db, turn)
    assert progress(db)["batches"] == 0


def test_duplicate_finish_is_idempotent_and_old_receipt_cannot_finish_next_batch(setup):
    db, _atlas = setup
    turn = new_turn(db)
    read(db, turn)
    acknowledge(db, turn)
    finish(db, turn)
    first = progress(db)
    finish(db, turn)
    assert progress(db) == first
    assert first["batches"] == 1 and len(first["pending"]) == 1


def test_restart_resumes_references_without_persisting_private_source_bodies(setup):
    db, atlas = setup
    atlas.handlers["root"] = lambda ref: {"data": {"secret": "PRIVATE ATLAS BODY"},
                                           "next_tasks": [task("child")], "continuation": None}
    turn = new_turn(db)
    result = read(db, turn)
    assert result["items"][0]["data"]["secret"] == "PRIVATE ATLAS BODY"
    assert "PRIVATE ATLAS BODY" not in json.dumps(progress(db))
    assert "PRIVATE ATLAS BODY" not in json.dumps(db.get(ActTurn, turn.id).assistant_review_json)
    # A failed attempt has not consumed its sources. A fresh session can read them again.
    finish(db, turn, "failed")
    with Session(db.bind) as restarted:
        retry = new_turn(restarted)
        output = read(restarted, retry)
        assert [item["reference"]["operation"] for item in output["items"]] == ["root", "child"]
        acknowledge(restarted, retry)
        finish(restarted, retry)
    assert progress(db)["batches"] == 1
    assert progress(db)["pending"] == [{"operation": "assistant.synthesize", "input": {}}]


def test_changed_source_starts_fresh_and_old_turn_cannot_advance_new_source(setup):
    db, atlas = setup
    old_turn = new_turn(db)
    read(db, old_turn)
    acknowledge(db, old_turn)
    atlas.identity = "atlas-two"
    atlas.tasks = [task("replacement")]
    assert AssistantInitializationService(db).status()["status"] == "pending"
    new = new_turn(db)
    assert read(db, new)["items"][0]["reference"] == task("replacement")
    before = progress(db)
    finish(db, old_turn)
    assert progress(db) == before
    acknowledge(db, new)
    finish(db, new)
    assert progress(db)["source"] == "atlas-two" and progress(db)["batches"] == 1


def test_read_failure_preserves_work_blocks_and_retries_after_fifteen_minutes(setup):
    db, atlas = setup

    def fail(reference):
        raise IntegrationError("atlas_locked", "Private provider explanation")

    atlas.handlers["root"] = fail
    turn = new_turn(db)
    before = datetime.now(UTC)
    with pytest.raises(ValueError, match="atlas_locked"):
        read(db, turn)
    state = progress(db)
    retry = datetime.fromisoformat(state["retry_at"])
    assert before + timedelta(minutes=14, seconds=59) < retry < datetime.now(UTC) + timedelta(minutes=15, seconds=1)
    assert state["pending"][0] == task("root") and state["batches"] == 0
    assert state["error"] == "atlas_locked" and "Private provider explanation" not in json.dumps(state)
    service = AssistantInitializationService(db)
    assert service.status()["status"] == "blocked"
    assert not service.ready(before + timedelta(minutes=14))
    assert service.ready(retry + timedelta(seconds=1))
    atlas.handlers.clear()
    assert read(db, turn)["phase"] == "reading"
    assert service.status()["error"] is None


def test_stale_paged_source_resets_cursor_without_discarding_other_pending_references(setup):
    db, atlas = setup
    atlas.tasks = [{"operation": "page", "input": {"cursor": "stale", "limit": 100}}, task("other")]

    def fail(reference):
        raise IntegrationError("stale_revision", "Page changed")

    atlas.handlers["page"] = fail
    turn = new_turn(db)
    with pytest.raises(ValueError, match="stale_revision"):
        read(db, turn)
    assert progress(db)["pending"] == [
        {"operation": "page", "input": {"limit": 100}}, task("other"),
        {"operation": "assistant.synthesize", "input": {}},
    ]


def test_ready_requires_connected_source_and_completed_state_waits_for_identity_change(setup):
    db, atlas = setup
    service = AssistantInitializationService(db)
    assert service.status() == {"status": "pending", "reviewed_batches": 0, "remaining": None, "error": None}
    atlas.connected = False
    assert not service.ready(datetime.now(UTC))
    atlas.connected = True
    assert service.ready(datetime.now(UTC))
    atlas.tasks = []
    turn = new_turn(db)
    assert read(db, turn)["phase"] == "synthesis"
    acknowledge(db, turn)
    finish(db, turn)
    assert not service.ready(datetime.now(UTC))
    atlas.identity = "new-atlas"
    assert service.ready(datetime.now(UTC))


def test_batches_bound_item_count_and_final_synthesis_remains_its_own_turn(setup):
    db, atlas = setup
    atlas.tasks = [task(f"item-{i}") for i in range(20)]
    sizes = []
    for expected in (8, 8, 4):
        turn = new_turn(db)
        result = read(db, turn)
        sizes.append(len(result["items"]))
        assert len(result["items"]) == expected and result["phase"] == "reading"
        assert all(item["reference"]["operation"] != "assistant.synthesize" for item in result["items"])
        acknowledge(db, turn)
        finish(db, turn)
    assert sizes == [8, 8, 4]
    assert len(atlas.calls) == 20 and len(progress(db)["pending"]) == 1


def test_batches_bound_json_characters(setup):
    db, atlas = setup
    atlas.tasks = [task("first"), task("second"), task("third")]
    atlas.handlers = {name: lambda ref: {"data": {"content": "a" * 25_000},
                                        "next_tasks": [], "continuation": None}
                      for name in ("first", "second", "third")}
    turn = new_turn(db)
    result = read(db, turn)
    assert len(result["items"]) == 1
    assert len(json.dumps(result["items"][0]["data"])) < 48_000
    acknowledge(db, turn)
    finish(db, turn)
    assert progress(db)["pending"][0] == task("second")


def test_images_get_own_batch_and_continuation_tasks_resume_exactly(setup):
    db, atlas = setup
    atlas.tasks = [task("first"), task("image"), task("chunk")]
    atlas.handlers["image"] = lambda ref: {
        "data": {"attachment": {"sha256": "a" * 64}}, "next_tasks": [], "continuation": None,
        "_initialization_image": {"data": "real-base64-image", "mime_type": "image/png"},
    }
    atlas.handlers["chunk"] = lambda ref: {
        "data": {"format": "json_slice", "content": "head" if "offset" not in ref else "tail"},
        "next_tasks": [] if "offset" not in ref else [task("child")],
        "continuation": {**ref, "offset": 24_000, "sha256": "b" * 64} if "offset" not in ref else None,
    }
    first = new_turn(db)
    assert [item["reference"]["operation"] for item in read(db, first)["items"]] == ["first"]
    acknowledge(db, first)
    finish(db, first)
    second = new_turn(db)
    image = read(db, second)
    assert len(image["items"]) == 1 and image["_initialization_image"]["mime_type"] == "image/png"
    assert "real-base64-image" not in json.dumps(progress(db))
    assert "real-base64-image" not in json.dumps(db.get(ActTurn, second.id).assistant_review_json)
    acknowledge(db, second)
    finish(db, second)
    third = new_turn(db)
    chunks = read(db, third)
    assert [item["data"].get("content") for item in chunks["items"]] == ["head", "tail", None]
    assert [item["reference"]["operation"] for item in chunks["items"]] == ["chunk", "chunk", "child"]
    acknowledge(db, third)
    finish(db, third)
    assert len(progress(db)["pending"]) == 1


def test_read_rejects_wrong_agent_wrong_session_cancelled_and_terminal_turns(setup):
    db, _atlas = setup
    turn = new_turn(db)
    service = AssistantInitializationService(db)
    with pytest.raises(ValueError, match="current running"):
        service.read(turn.session_id + 100, turn.id)
    turn.cancel_requested_at = datetime.now(UTC)
    db.commit()
    with pytest.raises(ValueError, match="current running"):
        read(db, turn)
    turn.cancel_requested_at = None
    turn.status = "succeeded"
    db.commit()
    with pytest.raises(ValueError, match="current running"):
        read(db, turn)


def test_initialization_schema_upgrade_preserves_understanding_and_is_idempotent(setup, monkeypatch):
    from sqlalchemy import inspect, text

    from app import db as db_module

    db, _atlas = setup
    AssistantService(db).state().summary = "Existing user understanding"
    db.commit()
    with db.bind.begin() as connection:
        connection.execute(text("ALTER TABLE assistant_state DROP COLUMN initialization_json"))
    monkeypatch.setattr(db_module, "engine", db.bind)
    db_module.ensure_local_schema()
    db_module.ensure_local_schema()
    assert "initialization_json" in {c["name"] for c in inspect(db.bind).get_columns("assistant_state")}
    db.expire_all()
    assert db.get(AssistantState, 1).initialization_json == {}
    assert db.get(AssistantState, 1).summary == "Existing user understanding"


def test_authenticated_initialization_builds_understanding_and_reuses_question_list(setup, monkeypatch):
    from app.services.agent_policy_service import AgentPolicyService
    from app.services.function_catalog_service import FunctionCatalogService
    from app.services.mcp_function_service import McpFunctionService

    db, atlas = setup
    monkeypatch.setattr(FunctionCatalogService, "list_entries", lambda *_a, **_k: [])
    atlas.handlers["root"] = lambda ref: {"data": {"goal": "Independent research", "motivation": None},
                                           "next_tasks": [], "continuation": None}
    turn = new_turn(db)
    token = AgentPolicyService(db).issue("assistant", turn.session_id)
    mcp = McpFunctionService(db, agent_token=token)

    def invoke(name, payload):
        return mcp.invoke(mcp.tool_name("agent_private", name), payload).output

    assert invoke("assistant.initialization.read", {})["items"][0]["data"]["goal"] == "Independent research"
    assert invoke("assistant.agenda.list", {"kind": "question", "active_only": False})["total"] == 0
    for title in ("What draws you to independent research?", "How does this fit your other priorities?",
                  "What outcome would make this worthwhile?"):
        invoke("assistant.agenda.create", {"kind": "question", "title": title,
            "notes": "Motivation missing from Atlas; understanding it would help evaluate alternatives."})
    assert invoke("assistant.initialization.acknowledge", {})["acknowledged"]
    finish(db, turn)
    assert progress(db)["batches"] == 1
    questions = [item for item in AssistantService(db).agenda() if item["kind"] == "question"]
    assert {item["title"] for item in questions} == {
        "What draws you to independent research?",
        "How does this fit your other priorities?",
        "What outcome would make this worthwhile?",
    }  # Question creation is independent of attention-aware asking times.
    assert len(questions) == 3
    assert mcp.tool_name("agent_private", "assistant.initialization.read") not in {
        entry.name for entry in McpFunctionService(db).list_tools()
    }
