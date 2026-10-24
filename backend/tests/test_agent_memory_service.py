import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.orm import Session

from app.db import Base
from app.models import ActLiveMessage, ActSession, ActTurn, AssistantState, MemoryFact
from app.routers import memory_facts
from app.schemas.memory_fact import MemoryFactCreate, MemoryFactRead, MemoryFactUpdate
from app.services import act_workspace_service, agent_memory_service
from app.services.act_workspace_service import ActWorkspaceError, ensure_act_workspace
from app.services.agent_memory_service import AgentMemoryService


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(act_workspace_service, "ACT_ROOT", tmp_path)
    return ensure_act_workspace()


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def archive_files(workspace):
    return sorted(workspace.memory.glob("chat-history/*/*.json"))


def archive_payloads(workspace):
    return [json.loads(path.read_text(encoding="utf-8")) for path in archive_files(workspace)]


@pytest.mark.parametrize("previously_seen", [False, True])
@pytest.mark.parametrize("during_sync", [False, True])
def test_archive_refreshes_turn_completed_by_another_session(workspace, db, monkeypatch, previously_seen, during_sync):
    now = datetime.now(UTC)
    session = ActSession(agent_id="act", codex_thread_id="concurrent-completion")
    db.add(session)
    db.flush()
    turn = ActTurn(session_id=session.id, user_message="Request", status="running", created_at=now)
    db.add(turn)
    db.commit()
    service = AgentMemoryService(db, workspace)
    if previously_seen:
        service.sync_chat_history(now=now)
    # A polling request holds a running ORM instance while the dispatcher
    # publishes the completed answer through its own database session.
    assert turn.status == "running"
    turn_id = turn.id

    def complete():
        with Session(db.bind) as dispatcher:
            dispatcher.execute(update(ActTurn).where(ActTurn.id == turn_id).values(
                status="succeeded", assistant_message="Complete answer", completed_at=now,
            ))
            dispatcher.commit()

    if during_sync:
        prune = service._prune_history

        def complete_before_cleanup(cutoff):
            complete()
            monkeypatch.setattr(service, "_prune_history", prune)
            prune(cutoff)

        monkeypatch.setattr(service, "_prune_history", complete_before_cleanup)
    else:
        complete()
    service.sync_chat_history(now=now)
    assert service.read_turn_content(turn)["assistant_message"] == "Complete answer"
    assert turn.assistant_message is None
    assert turn.assistant_review_json["chat_history"]["has_assistant_response"] is True


def test_workspace_creates_only_fact_summary_history_directories_and_preserves_guidance(workspace):
    agents = workspace.root / "AGENTS.md"
    agents.write_text("# My instructions\nKeep my files.\n", encoding="utf-8")
    ensure_act_workspace()
    once = agents.read_text(encoding="utf-8")
    ensure_act_workspace()
    assert agents.read_text(encoding="utf-8") == once
    assert once.startswith("# My instructions\nKeep my files.")
    assert "30 days and 50 MiB" in once
    assert "memory/daily-summaries/YYYY-MM-DD.md" in once
    assert sorted(path.name for path in workspace.memory.iterdir() if path.is_dir()) == [
        "chat-history", "daily-summaries", "facts-outside-atlas",
    ]


def test_workspace_upgrades_legacy_transcript_ban_and_keeps_unrelated_rules(workspace):
    agents = workspace.root / "AGENTS.md"
    agents.write_text("# User guidance\n" + act_workspace_service._LEGACY_MEMORY_GUIDANCE
                      + "\nNever modify knowledge/.\n", encoding="utf-8")
    ensure_act_workspace()
    once = agents.read_text(encoding="utf-8")
    assert "Never store transcripts" not in once
    assert "Never modify knowledge/." in once
    assert "bounded full chat history" in once
    ensure_act_workspace()
    assert agents.read_text(encoding="utf-8") == once


def test_fact_crud_reads_user_file_edits_and_checks_expected_value(workspace):
    service = AgentMemoryService(workspace=workspace)
    fact = service.create_fact({"key": "extra", "value": "Original", "category": "preferences",
                                "provenance_json": {"attribution": "stated", "evidence": "User said it"}})
    assert fact["category"] == "facts"
    assert fact["legacy_category"] == "preferences"
    path = service.facts / f"{fact['id']}.json"
    edited = {**fact, "value": "Edited in a text editor"}
    path.write_text(json.dumps(edited), encoding="utf-8")
    assert service.list_facts()[0]["value"] == edited["value"]
    with pytest.raises(ValueError, match="Memory changed"):
        service.update_fact(fact["id"], {"value": "Stale edit"}, expected_value="Original")
    updated = service.update_fact(fact["id"], {"value": "Correction"}, expected_value=edited["value"])
    assert updated["provenance_json"] == fact["provenance_json"]
    assert updated["created_at"] == fact["created_at"]
    service.delete_fact(fact["id"])
    assert service.list_facts() == []
    later = service.create_fact({"key": "later", "value": "Do not reuse deleted ID"})
    assert later["id"] > fact["id"]
    with pytest.raises(LookupError):
        service.get_fact(fact["id"])


def test_concurrent_fact_creates_allocate_unique_ids(workspace):
    def create(index):
        return AgentMemoryService(workspace=workspace).create_fact({"key": f"key-{index}", "value": "value"})

    with ThreadPoolExecutor(max_workers=4) as pool:
        facts = list(pool.map(create, range(16)))
    assert len({fact["id"] for fact in facts}) == 16
    assert len(AgentMemoryService(workspace=workspace).list_facts()) == 16


def test_legacy_migration_preserves_ids_provenance_timestamps_and_user_deletion(workspace, db):
    timestamp = datetime(2026, 10, 1, tzinfo=UTC)
    fact = MemoryFact(id=9, key="legacy", value="Original fact", category="writing_style",
                      provenance_json={"attribution": "stated", "evidence": "Conversation"},
                      created_at=timestamp, updated_at=timestamp)
    db.add_all([fact, AssistantState(id=1, summary="Old useful continuity")])
    db.commit()
    service = AgentMemoryService(db, workspace)
    service.migrate_legacy()
    assert list(db.scalars(select(MemoryFact))) == []
    migrated = service.get_fact(9)
    assert migrated["created_at"] == timestamp.isoformat()
    assert migrated["provenance_json"]["evidence"] == "Conversation"
    assert migrated["legacy_category"] == "writing_style"
    assert "Old useful continuity" in (service.summaries / "legacy-continuity.md").read_text()
    assert db.get(AssistantState, 1).summary == ""
    assert service.list_daily_summaries() == []
    service.delete_fact(9)
    (service.summaries / "legacy-continuity.md").unlink()
    service.migrate_legacy()
    assert service.list_facts() == []
    assert not (service.summaries / "legacy-continuity.md").exists()
    assert service.create_fact({"key": "next", "value": "New fact"})["id"] == 10


def test_migration_recovers_write_before_commit_without_overwriting_edits(workspace, db, monkeypatch):
    db.add(MemoryFact(id=4, key="legacy", value="Original", category="goals"))
    db.commit()
    service = AgentMemoryService(db, workspace)
    commit = db.commit
    monkeypatch.setattr(db, "commit", lambda: (_ for _ in ()).throw(RuntimeError("interrupted")))
    with pytest.raises(RuntimeError, match="interrupted"):
        service.migrate_legacy()
    db.rollback()
    monkeypatch.setattr(db, "commit", commit)
    service.update_fact(4, {"value": "User correction after interruption"})
    service.migrate_legacy()
    assert service.get_fact(4)["value"] == "User correction after interruption"
    assert db.get(MemoryFact, 4) is None


def test_migration_never_deletes_database_row_after_failed_file_write(workspace, db, monkeypatch):
    db.add(MemoryFact(id=4, key="legacy", value="Original", category="goals"))
    db.commit()
    service = AgentMemoryService(db, workspace)
    monkeypatch.setattr(service, "_write_json", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        service.migrate_legacy()
    db.rollback()
    assert db.get(MemoryFact, 4).value == "Original"


def test_daily_summary_has_receipt_is_short_and_does_not_rewrite_user_edits(workspace):
    service = AgentMemoryService(workspace=workspace)
    written = service.write_daily_summary("2026-10-17", "Decided to use one memory folder.", source_turn_id=7)
    assert written["source_turn_id"] == 7
    path = Path(written["path"])
    path.write_text(path.read_text().replace("Decided", "Confirmed"), encoding="utf-8")
    retried = service.write_daily_summary("2026-10-17", "A repeated tool call", source_turn_id=7)
    assert retried["summary"].startswith("Confirmed")
    assert service.list_daily_summaries()[0] == retried
    assert service.get_daily_summary("2026-10-16") is None
    with pytest.raises(ValueError, match="1200"):
        service.write_daily_summary("2026-10-17", "x" * 1201)
    with pytest.raises(ValueError):
        service.write_daily_summary("../../outside", "Escape")
    for empty in ("", " ", "\t\n"):
        with pytest.raises(ValueError, match="nothing important"):
            service.write_daily_summary("2026-10-18", empty)
    assert service.get_daily_summary("2026-10-18") is None


def test_archive_contains_full_public_messages_for_three_agents_and_live_replies(workspace, db):
    now = datetime(2026, 10, 17, 18, tzinfo=UTC)
    sessions = [ActSession(agent_id=agent, codex_thread_id=agent) for agent in ("assistant", "act", "observer")]
    db.add_all(sessions)
    db.flush()
    turns = [ActTurn(session_id=session.id, user_message=f"Full {session.agent_id} request\n" + "long " * 4000,
                     assistant_message=f"Complete {session.agent_id} answer", status="succeeded",
                     created_at=now, completed_at=now + timedelta(minutes=1),
                     activity_json=[{"private_reasoning": "never export"}]) for session in sessions]
    db.add_all(turns)
    db.flush()
    db.add_all([ActTurn(session_id=sessions[0].id, user_message="PRIVATE ACTIVATION",
                        assistant_context_json={"reasons": ["daily"]}, assistant_message="Public review result",
                        status="succeeded", created_at=now, completed_at=now),
                ActTurn(session_id=sessions[0].id, user_message="HIDDEN EMPTY CONTEXT", assistant_context_json={},
                        status="succeeded", created_at=now, completed_at=now),
                ActLiveMessage(turn_id=turns[0].id, role="assistant", text="Live question", created_at=now),
                ActLiveMessage(turn_id=turns[0].id, role="user", text="Live answer", created_at=now)])
    db.commit()
    service = AgentMemoryService(db, workspace)
    original_user = turns[0].user_message
    service.sync_chat_history(now=now + timedelta(minutes=2))
    payloads = archive_payloads(workspace)
    assert len(payloads) == 9
    assert {payload["agent"] for payload in payloads} == {"assistant", "act", "observer"}
    assert any(payload["text"] == original_user for payload in payloads)
    for turn in turns:
        assert turn.user_message == "" and turn.assistant_message is None
        assert turn.history_archived_at is not None
        assert turn.activity_json == [{"private_reasoning": "never export"}]
        content = service.read_turn_content(turn)
        assert content["available"] and content["user_message"].startswith("Full ")
        assert content["assistant_message"].startswith("Complete ")
    assert service.read_turn_content(turns[0])["live_messages"]
    assert all(message.text == "" for message in db.scalars(select(ActLiveMessage)))
    assert all("PRIVATE ACTIVATION" not in payload["text"] for payload in payloads)
    assert all("HIDDEN EMPTY CONTEXT" not in payload["text"] for payload in payloads)
    assert all("private_reasoning" not in payload for payload in payloads)
    assert {payload["text"] for payload in payloads if payload["source"] == "live"} == {"Live question", "Live answer"}
    assert service.read_chat_history("2026-10-17", "America/Toronto")["total_messages"] == 9
    bounded = service.read_chat_history("2026-10-17", "America/Toronto", max_characters=1000)
    assert bounded["truncated"]
    assert all(not payload["text"].startswith("Full ") for payload in bounded["messages"])


def test_running_turn_completion_exports_once_and_manual_archive_deletion_is_respected(workspace, db):
    now = datetime(2026, 10, 17, 18, tzinfo=UTC)
    session = ActSession(agent_id="assistant", codex_thread_id="running")
    db.add(session)
    db.flush()
    turn = ActTurn(session_id=session.id, user_message="Question", status="running", created_at=now)
    db.add(turn)
    db.commit()
    service = AgentMemoryService(db, workspace)
    service.sync_chat_history(now=now)
    assert len(archive_files(workspace)) == 1
    assert turn.user_message == "Question" and turn.history_archived_at is None
    archive_files(workspace)[0].unlink()
    turn.assistant_message = "Final answer"
    turn.status = "succeeded"
    turn.completed_at = now + timedelta(minutes=1)
    db.commit()
    service.sync_chat_history(now=now + timedelta(minutes=1))
    service.sync_chat_history(now=now + timedelta(minutes=1))
    assert [payload["text"] for payload in archive_payloads(workspace)] == ["Final answer"]
    assert service.read_turn_content(turn) == {"user_message": "", "assistant_message": "Final answer",
                                              "live_messages": {}, "available": True}


def test_retention_removes_oldest_whole_messages_and_never_reimports(workspace, db, monkeypatch):
    now = datetime(2026, 10, 17, 18, tzinfo=UTC)
    session = ActSession(agent_id="act", codex_thread_id="retention")
    db.add(session)
    db.flush()
    for index, days in enumerate((31, 2, 1, 0)):
        db.add(ActTurn(session_id=session.id, user_message=f"Request {index}", assistant_message=f"Answer {index}",
                       status="succeeded", created_at=now - timedelta(days=days),
                       completed_at=now - timedelta(days=days)))
    db.commit()
    service = AgentMemoryService(db, workspace)
    service.sync_chat_history(now=now)
    assert len(archive_files(workspace)) == 6
    assert all(" 0" not in payload["text"] for payload in archive_payloads(workspace))
    budget = sum(path.stat().st_size for path in archive_files(workspace)[-2:])
    monkeypatch.setattr(agent_memory_service, "CHAT_HISTORY_MAX_BYTES", budget)
    service.sync_chat_history(now=now)
    assert len(archive_files(workspace)) == 2
    assert all(" 3" in payload["text"] for payload in archive_payloads(workspace))
    monkeypatch.setattr(agent_memory_service, "CHAT_HISTORY_MAX_BYTES", 50 * 1024 * 1024)
    service.sync_chat_history(now=now)
    assert len(archive_files(workspace)) == 2
    service.sync_chat_history(now=now + timedelta(days=31))
    assert archive_files(workspace) == []
    for turn in db.scalars(select(ActTurn)):
        assert turn.user_message == "" and turn.assistant_message is None and turn.history_archived_at
        assert service.read_turn_content(turn)["available"] is False


def test_archive_handles_sqlite_ids_reused_after_topic_deletion(workspace, db):
    now = datetime(2026, 10, 17, 18, tzinfo=UTC)
    session = ActSession(agent_id="act", codex_thread_id="original-topic")
    db.add(session)
    db.flush()
    original = ActTurn(session_id=session.id, user_message="Old topic request", status="succeeded",
                       created_at=now)
    db.add(original)
    db.flush()
    db.add(ActLiveMessage(turn_id=original.id, role="assistant", text="Old live answer", created_at=now))
    db.commit()
    original_id = original.id
    service = AgentMemoryService(db, workspace)
    service.sync_chat_history(now=now)
    db.execute(delete(ActLiveMessage))
    db.execute(delete(ActTurn))
    db.commit()
    newer = ActTurn(session_id=session.id, user_message="New topic request", status="succeeded",
                    created_at=now + timedelta(seconds=1))
    db.add(newer)
    db.flush()
    assert newer.id == original_id
    db.add(ActLiveMessage(turn_id=newer.id, role="assistant", text="New live answer",
                          created_at=now + timedelta(seconds=1)))
    db.commit()
    service.sync_chat_history(now=now + timedelta(seconds=1))
    assert {payload["text"] for payload in archive_payloads(workspace)} == {
        "Old topic request", "Old live answer", "New topic request", "New live answer",
    }


@pytest.mark.parametrize("initial_status", ["failed", "running"])
def test_reflection_response_is_not_chat_history_even_when_failed(workspace, db, initial_status):
    now = datetime(2026, 10, 17, 18, tzinfo=UTC)
    session = ActSession(agent_id="assistant", codex_thread_id="reflection")
    db.add(session)
    db.flush()
    turn = ActTurn(session_id=session.id, user_message="Private reflection instruction",
                   assistant_message="This summary belongs only in daily-summaries.",
                   assistant_context_json={"kind": "reflection", "reflection": True},
                   status=initial_status, created_at=now, completed_at=now)
    db.add(turn)
    db.commit()
    service = AgentMemoryService(db, workspace)
    service.sync_chat_history(now=now)
    if initial_status == "running":
        turn.status = "failed"
        db.commit()
        service.sync_chat_history(now=now)
    assert archive_files(workspace) == []


def test_user_refresh_command_and_visible_acknowledgement_are_full_chat_history(workspace, db):
    now = datetime(2026, 10, 17, 18, tzinfo=UTC)
    session = ActSession(agent_id="assistant", codex_thread_id="refresh-control")
    db.add(session)
    db.flush()
    answer = "Assistant session will refresh before the next turn."
    db.add(ActTurn(session_id=session.id, user_message="/refresh", assistant_message=answer,
                   backend_message_kind="assistant_refresh", status="succeeded",
                   created_at=now, completed_at=now))
    db.commit()
    AgentMemoryService(db, workspace).sync_chat_history(now=now)
    assert {(payload["role"], payload["text"]) for payload in archive_payloads(workspace)} == {
        ("user", "/refresh"), ("assistant", answer),
    }


def test_one_message_larger_than_archive_budget_is_discarded_whole(workspace, db, monkeypatch):
    now = datetime(2026, 10, 17, 18, tzinfo=UTC)
    session = ActSession(agent_id="act", codex_thread_id="large-message")
    db.add(session)
    db.flush()
    db.add(ActTurn(session_id=session.id, user_message="Full text must never be truncated. " * 1000,
                   status="succeeded", created_at=now))
    db.commit()
    service = AgentMemoryService(db, workspace)
    monkeypatch.setattr(agent_memory_service, "CHAT_HISTORY_MAX_BYTES", 500)
    service.sync_chat_history(now=now)
    assert archive_files(workspace) == []
    monkeypatch.setattr(agent_memory_service, "CHAT_HISTORY_MAX_BYTES", 50 * 1024 * 1024)
    service.sync_chat_history(now=now)
    assert archive_files(workspace) == []


def test_memory_routes_use_files_and_expose_generic_facts(workspace, db):
    created = memory_facts.create_memory_fact(MemoryFactCreate(key="outside", value="A fact"), db)
    assert created["category"] == "facts"
    assert MemoryFactRead.model_validate(created).value == "A fact"
    assert list(db.scalars(select(MemoryFact))) == []
    updated = memory_facts.update_memory_fact(created["id"], MemoryFactUpdate(value="Corrected fact"), db)
    assert updated["provenance_json"]["attribution"] == "stated"
    assert memory_facts.get_memory_fact(created["id"], db)["value"] == "Corrected fact"
    assert len(memory_facts.list_memory_facts(db)) == 1
    memory_facts.delete_memory_fact(created["id"], db)
    assert memory_facts.list_memory_facts(db) == []


def test_archive_failure_keeps_accepted_chat_and_retries_full_message(workspace, db, monkeypatch, caplog):
    from app.services.act_session_service import ActSessionService
    from app.services.agent_dispatchers import agent_dispatchers

    monkeypatch.setattr(ActSessionService, "synchronize_telegram", lambda *_: None)
    notified = []
    monkeypatch.setattr(agent_dispatchers["act"], "notify", lambda: notified.append(True))
    sessions = ActSessionService(db)
    session = sessions.create_session()
    original = AgentMemoryService.sync_chat_history

    def unavailable(*args, **kwargs):
        raise OSError("Archive temporarily unavailable")

    monkeypatch.setattr(AgentMemoryService, "sync_chat_history", unavailable)
    text = "The complete accepted chat message. " * 300
    turn = sessions.enqueue_turn(session.id, text)
    assert turn.status == "queued" and turn.user_message == text and notified == [True]
    assert db.get(ActTurn, turn.id).user_message == text
    assert "next synchronization will retry" in caplog.text
    monkeypatch.setattr(AgentMemoryService, "sync_chat_history", original)
    AgentMemoryService(db, workspace).sync_chat_history()
    assert [payload["text"] for payload in archive_payloads(workspace)] == [text]


def test_rejects_linked_memory_files_without_reading_or_overwriting_outside(workspace, tmp_path):
    service = AgentMemoryService(workspace=workspace)
    outside = tmp_path / "outside.json"
    outside.write_text('{"id": 1, "key": "secret", "value": "outside"}', encoding="utf-8")
    link = service.facts / "1.json"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Creating symbolic links is unavailable")
    with pytest.raises(ActWorkspaceError, match="symbolic"):
        service.list_facts()
    with pytest.raises(ActWorkspaceError, match="symbolic"):
        service.update_fact(1, {"value": "overwrite"})
    assert "outside" in outside.read_text()


def test_archive_file_failure_leaves_completed_database_content_for_retry(workspace, db, monkeypatch):
    now = datetime.now(UTC)
    session = ActSession(agent_id="act", codex_thread_id="write-failure")
    db.add(session)
    db.flush()
    turn = ActTurn(session_id=session.id, user_message="Original request", assistant_message="Full answer",
                   status="succeeded", created_at=now, completed_at=now)
    db.add(turn)
    db.commit()
    service = AgentMemoryService(db, workspace)
    original = agent_memory_service.atomic_workspace_write
    monkeypatch.setattr(agent_memory_service, "atomic_workspace_write",
                        lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        service.sync_chat_history(now=now)
    db.expire_all()
    assert turn.user_message == "Original request" and turn.assistant_message == "Full answer"
    assert turn.history_archived_at is None
    monkeypatch.setattr(agent_memory_service, "atomic_workspace_write", original)
    service.sync_chat_history(now=now)
    assert turn.history_archived_at and turn.user_message == "" and turn.assistant_message is None
    assert service.read_turn_content(turn)["assistant_message"] == "Full answer"


def test_archive_sql_commit_retry_respects_user_deletion_and_edited_text(workspace, db, monkeypatch):
    now = datetime.now(UTC)
    session = ActSession(agent_id="observer", codex_thread_id="commit-failure")
    db.add(session)
    db.flush()
    turn = ActTurn(session_id=session.id, user_message="Discarded by user", assistant_message="Original response",
                   status="failed", created_at=now, completed_at=now, error_message="Failure metadata",
                   assistant_review_json={"important": "receipt"}, delivery_status="pending")
    db.add(turn)
    db.commit()
    service = AgentMemoryService(db, workspace)
    commit = db.commit
    monkeypatch.setattr(db, "commit", lambda: (_ for _ in ()).throw(RuntimeError("SQL interrupted")))
    with pytest.raises(RuntimeError, match="SQL interrupted"):
        service.sync_chat_history(now=now)
    assert turn.history_archived_at is None and turn.assistant_message == "Original response"
    for path in archive_files(workspace):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload["role"] == "user":
            path.unlink()
        else:
            path.write_text(json.dumps({**payload, "text": "User-edited response"}), encoding="utf-8")
    monkeypatch.setattr(db, "commit", commit)
    service.sync_chat_history(now=now)
    assert len(archive_files(workspace)) == 1
    assert service.read_turn_content(turn) == {"user_message": "", "assistant_message": "User-edited response",
                                              "live_messages": {}, "available": True}
    assert turn.assistant_message is None and turn.user_message == ""
    assert turn.error_message == "Failure metadata"
    assert turn.assistant_review_json == {"important": "receipt", "chat_history": {"has_assistant_response": True}}
    assert turn.delivery_status == "pending"
    archive_files(workspace)[0].unlink()
    # The marker forbids a stale database value from serving as a fallback.
    turn.assistant_message = "Stale duplicate from an old backend"
    db.commit()
    assert service.read_turn_content(turn)["assistant_message"] is None
    assert service.read_turn_content(turn)["available"] is False
    service.sync_chat_history(now=now)
    assert archive_files(workspace) == []
    assert turn.assistant_message is None


def test_archive_read_rejects_identity_edits_and_id_reuse(workspace, db):
    now = datetime.now(UTC)
    session = ActSession(agent_id="act", codex_thread_id="identity")
    db.add(session)
    db.flush()
    turn = ActTurn(session_id=session.id, user_message="Old content", status="succeeded", created_at=now)
    db.add(turn)
    db.commit()
    service = AgentMemoryService(db, workspace)
    service.sync_chat_history(now=now)
    path = archive_files(workspace)[0]
    payload = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**payload, "session_id": session.id + 1}), encoding="utf-8")
    with pytest.raises(ValueError, match="identity"):
        service.read_turn_content(turn)
    path.write_text(json.dumps(payload), encoding="utf-8")
    original_id = turn.id
    db.delete(turn)
    db.commit()
    newer = ActTurn(session_id=session.id, user_message="New content", status="succeeded",
                    created_at=now + timedelta(seconds=1))
    db.add(newer)
    db.commit()
    assert newer.id == original_id
    service.sync_chat_history(now=now + timedelta(seconds=1))
    assert service.read_turn_content(newer)["user_message"] == "New content"


def test_hidden_completed_prompts_are_cleared_and_running_live_text_is_untouched(workspace, db):
    now = datetime.now(UTC)
    session = ActSession(agent_id="assistant", codex_thread_id="hidden")
    db.add(session)
    db.flush()
    reflection = ActTurn(session_id=session.id, user_message="Private instruction", assistant_message="Private reflection",
                         assistant_context_json={"kind": "reflection"}, status="failed", created_at=now)
    running = ActTurn(session_id=session.id, user_message="Active instruction", assistant_message="Partial response",
                      status="running", created_at=now)
    db.add_all([reflection, running])
    db.flush()
    hidden = ActLiveMessage(turn_id=reflection.id, role="assistant", text="Private live output", created_at=now)
    active = ActLiveMessage(turn_id=running.id, role="user", text="Active reply", created_at=now)
    db.add_all([hidden, active])
    db.commit()
    service = AgentMemoryService(db, workspace)
    service.sync_chat_history(now=now)
    assert reflection.history_archived_at and reflection.user_message == "" and reflection.assistant_message is None
    assert hidden.text == "" and service.read_turn_content(reflection)["available"] is False
    assert running.history_archived_at is None and running.user_message == "Active instruction"
    assert running.assistant_message == "Partial response" and active.text == "Active reply"
    assert service.read_turn_content(running)["live_messages"] == {active.id: "Active reply"}


def test_new_backdated_turn_and_live_message_are_not_skipped_by_archive_cursor(workspace, db):
    now = datetime.now(UTC)
    session = ActSession(agent_id="act", codex_thread_id="clock-rollback")
    db.add(session)
    db.flush()
    first = ActTurn(session_id=session.id, user_message="Before clock rollback", status="succeeded", created_at=now)
    db.add(first)
    db.commit()
    service = AgentMemoryService(db, workspace)
    service.sync_chat_history(now=now)
    backdated = ActTurn(session_id=session.id, user_message="After clock rollback", assistant_message="Backdated answer",
                        status="succeeded", created_at=now - timedelta(hours=1), completed_at=now - timedelta(hours=1))
    db.add(backdated)
    db.flush()
    reply = ActLiveMessage(turn_id=backdated.id, role="user", text="Backdated live reply",
                           created_at=now - timedelta(hours=1))
    db.add(reply)
    db.commit()
    service.sync_chat_history(now=now)
    assert service.read_turn_content(backdated) == {
        "user_message": "After clock rollback", "assistant_message": "Backdated answer",
        "live_messages": {reply.id: "Backdated live reply"}, "available": True,
    }
    assert backdated.history_archived_at and backdated.user_message == "" and reply.text == ""
    service.sync_chat_history(now=now)
    assert len(archive_files(workspace)) == 4


def test_reused_turn_and_live_ids_after_clock_rollback_still_archive_new_content(workspace, db):
    now = datetime.now(UTC)
    session = ActSession(agent_id="act", codex_thread_id="rollback-reuse")
    db.add(session)
    db.flush()
    original = ActTurn(session_id=session.id, user_message="Old request", status="succeeded", created_at=now)
    db.add(original)
    db.flush()
    original_id = original.id
    db.add(ActLiveMessage(turn_id=original.id, role="assistant", text="Old live", created_at=now))
    db.commit()
    service = AgentMemoryService(db, workspace)
    service.sync_chat_history(now=now)
    db.execute(delete(ActLiveMessage))
    db.execute(delete(ActTurn))
    db.commit()
    session_id = session.id
    db.expunge_all()
    newer = ActTurn(session_id=session_id, user_message="New request", assistant_message="New answer", status="succeeded",
                    created_at=now - timedelta(hours=1), completed_at=now - timedelta(minutes=59))
    db.add(newer)
    db.flush()
    assert newer.id == original_id
    live = ActLiveMessage(turn_id=newer.id, role="assistant", text="New live", created_at=now - timedelta(hours=1))
    db.add(live)
    db.commit()
    service.sync_chat_history(now=now)
    assert service.read_turn_content(newer) == {"user_message": "New request", "assistant_message": "New answer",
                                               "live_messages": {live.id: "New live"}, "available": True}
    assert newer.user_message == "" and newer.assistant_message is None and live.text == ""


def test_active_live_file_deletion_survives_terminal_commit_interruption(workspace, db, monkeypatch):
    now = datetime.now(UTC)
    session = ActSession(agent_id="act", codex_thread_id="active-manual-delete")
    db.add(session)
    db.flush()
    turn = ActTurn(session_id=session.id, user_message="Delete my question archive", status="running", created_at=now)
    db.add(turn)
    db.flush()
    live = ActLiveMessage(turn_id=turn.id, role="user", text="Delete my live reply archive", created_at=now)
    db.add(live)
    db.commit()
    service = AgentMemoryService(db, workspace)
    service.sync_chat_history(now=now)
    for path in archive_files(workspace):
        path.unlink()
    turn.status = "succeeded"
    turn.assistant_message = "Retained final answer"
    turn.completed_at = now + timedelta(seconds=1)
    db.commit()
    commit = db.commit
    monkeypatch.setattr(db, "commit", lambda: (_ for _ in ()).throw(RuntimeError("SQL interrupted")))
    with pytest.raises(RuntimeError, match="SQL interrupted"):
        service.sync_chat_history(now=now + timedelta(seconds=1))
    monkeypatch.setattr(db, "commit", commit)
    service.sync_chat_history(now=now + timedelta(seconds=1))
    assert service.read_turn_content(turn) == {"user_message": "", "assistant_message": "Retained final answer",
                                              "live_messages": {}, "available": True}
    assert [payload["text"] for payload in archive_payloads(workspace)] == ["Retained final answer"]


def test_receipt_capacity_failure_never_clears_unsaved_database_content(workspace, db, monkeypatch):
    now = datetime.now(UTC)
    session = ActSession(agent_id="act", codex_thread_id="receipt-capacity")
    db.add(session)
    db.flush()
    turn = ActTurn(session_id=session.id, user_message="Keep until acknowledged", status="succeeded", created_at=now)
    db.add(turn)
    db.commit()
    service = AgentMemoryService(db, workspace)
    monkeypatch.setattr(agent_memory_service, "MAX_METADATA_BYTES", 20)
    with pytest.raises(ValueError, match="bookkeeping exceeds"):
        service.sync_chat_history(now=now)
    db.expire_all()
    assert turn.user_message == "Keep until acknowledged" and turn.history_archived_at is None


def test_legacy_archive_cursor_migration_respects_already_deleted_history(workspace, db):
    now = datetime.now(UTC)
    session = ActSession(agent_id="act", codex_thread_id="legacy-cursor")
    db.add(session)
    db.flush()
    turn = ActTurn(session_id=session.id, user_message="Old retained duplicate", assistant_message="Old answer",
                   status="succeeded", created_at=now, completed_at=now)
    db.add(turn)
    db.commit()
    service = AgentMemoryService(db, workspace)
    service.sync_chat_history(now=now)
    # Represent the previous version: durable global receipts and duplicate SQL
    # text, but neither the new archive marker nor per-turn cleanup receipts.
    turn.user_message = "Old retained duplicate"
    turn.assistant_message = "Old answer"
    turn.history_archived_at = None
    db.commit()
    state = json.loads(service.metadata.read_text(encoding="utf-8"))
    state["chat_history"].pop("completed_content_migrated")
    state["chat_history"].pop("cleanup_turn_keys")
    service.metadata.write_text(json.dumps(state), encoding="utf-8")
    for path in archive_files(workspace):
        path.unlink()
    service.sync_chat_history(now=now)
    assert turn.history_archived_at and turn.user_message == "" and turn.assistant_message is None
    assert service.read_turn_content(turn)["available"] is False
    assert archive_files(workspace) == []
