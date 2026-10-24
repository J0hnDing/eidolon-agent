from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import ActSession, AgentPolicy, IntegrationConnection
from app.services.agent_memory_service import AgentMemoryService
from app.services.assistant_sources import available_sources, collect_sources
from app.services.integration_service import IntegrationError


class Sources:
    def __init__(self):
        self.connected = set()
        self.calls = []
        self.handlers = {}
        self.connection_id = 1
        self.account_id = "test-account"

    def _connection(self, provider):
        if provider not in self.connected:
            return None
        return SimpleNamespace(id=self.connection_id, account_id=self.account_id,
                               configured_resource_id="test-todos")

    def provider_connected(self, provider):
        return provider in self.connected

    def execute_context(self, context, operation, arguments):
        assert context.agent_id == "assistant"
        assert context.agent_session_id == 17
        self.calls.append((operation, arguments))
        return SimpleNamespace(output=self.handlers[operation](arguments))


@pytest.fixture
def setup_sources(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    source = Sources()
    monkeypatch.setattr("app.services.assistant_sources.build_default_integration_service", lambda db: source)
    with Session(engine) as db:
        db.add(ActSession(id=17, agent_id="assistant", codex_thread_id="pending:source-tests"))
        db.commit()
        yield db, source
    engine.dispose()


def source_named(snapshots, name):
    return next(value for key, value in snapshots.items() if key.startswith(name + ":"))


def test_atlas_complete_records_and_links_keep_only_metadata(setup_sources):
    db, source = setup_sources
    source.connected.add("atlas")
    record = {"id": "person-1", "category": "person", "title": "Me", "revision": 2,
              "data": {"sensitive": "never persist this"}}
    link = {"id": "link-1", "sourceId": "person-1", "targetId": "goal-1", "revision": 1,
            "data": {"private": "private explanation"}}
    source.handlers["atlas.records.search"] = lambda args: {
        "items": [{key: value for key, value in record.items() if key != "data"}], "next_cursor": None,
    }
    source.handlers["atlas.records.get"] = lambda args: {"record": {**record, "links": [link], "backlinks": []}}
    snapshots = collect_sources(db, session_id=17)
    records = source_named(snapshots, "atlas.records")
    links = source_named(snapshots, "atlas.links")
    assert records["complete"] and links["complete"]
    assert records["items"]["person-1"]["revision"] == 2
    assert links["items"]["link-1"]["reference"] == {
        "operation": "atlas.links.get", "input": {"id": "link-1"},
    }
    assert "never persist this" not in str(snapshots)
    assert "private explanation" not in str(snapshots)
    first_hash = records["items"]["person-1"]["content_hash"]
    assert source.calls[0][1]["include_data"] is False
    record["data"]["sensitive"] = "changed secret"
    record["revision"] += 1
    assert source_named(collect_sources(db, session_id=17), "atlas.records")["items"]["person-1"]["content_hash"] != first_hash


def test_all_notion_pages_and_no_notes_persisted(setup_sources):
    db, source = setup_sources
    source.connected.add("notion")
    source.handlers["notion.todo.list"] = lambda args: {
        "todos": [{"id": "two", "title": "Second", "notes": "private notes"}] if args.get("start_cursor")
        else [{"id": "one", "title": "First"}],
        "next_cursor": None if args.get("start_cursor") else "page-two",
        "has_more": not bool(args.get("start_cursor")),
    }
    result = source_named(collect_sources(db, session_id=17), "notion.todos")
    assert result["complete"] and set(result["items"]) == {"one", "two"}
    assert "private notes" not in str(result)
    assert source.calls[-1][1]["start_cursor"] == "page-two"


@pytest.mark.parametrize("failure", ["timeout", "cycle", "missing_cursor"])
def test_partial_or_invalid_pagination_never_claims_complete(setup_sources, failure):
    db, source = setup_sources
    source.connected.add("notion")

    def read(args):
        if failure == "timeout" and args.get("start_cursor"):
            raise IntegrationError("provider_timeout", "Secret provider details")
        return {"todos": [{"id": "one", "title": "First"}], "has_more": True,
                "next_cursor": None if failure == "missing_cursor" else "repeated"}

    source.handlers["notion.todo.list"] = read
    result = source_named(collect_sources(db, session_id=17), "notion.todos")
    assert result["items"] == {} and not result["complete"]
    assert result["error"] in {"provider_timeout", "provider_unavailable"}
    assert "Secret provider details" not in str(result)


def test_unavailable_sources_do_not_claim_empty_success_or_make_requests(setup_sources):
    db, source = setup_sources
    snapshots = collect_sources(db, session_id=17)
    assert source.calls == []
    for key, result in snapshots.items():
        if key == "memory":
            continue
        assert not result["complete"] and result["error"] == "connection_unavailable"


def test_missing_assistant_session_does_not_run_integrations(setup_sources):
    db, source = setup_sources
    source.connected.add("notion")
    snapshots = collect_sources(db)
    assert source.calls == []
    assert source_named(snapshots, "notion.todos")["error"] == "agent_permission_denied"


def test_permissions_failure_isolated_from_other_sources(setup_sources):
    db, source = setup_sources
    source.connected.update({"notion", "google_calendar"})

    def denied(args):
        raise IntegrationError("agent_permission_denied", "Explicitly banned")

    source.handlers["notion.todo.list"] = denied
    source.handlers["google_calendar.event.list"] = lambda args: {
        "events": [], "next_page_token": None, "has_more": False,
    }
    snapshots = collect_sources(db, session_id=17)
    assert source_named(snapshots, "notion.todos")["error"] == "agent_permission_denied"
    assert source_named(snapshots, "calendar.events")["complete"]


def test_provider_error_inside_email_result_is_not_empty_success(setup_sources):
    db, source = setup_sources
    source.connected.add("gmail")
    source.handlers["email.search"] = lambda args: {
        "conversations": [], "provider_errors": [{"error_type": "rate_limited"}],
        "has_more": False, "next_page_token": None,
    }
    assert source_named(collect_sources(db, session_id=17), "email.gmail")["error"] == "rate_limited"


def test_calendar_scope_is_stable_within_month_and_all_pages_reuse_bounds(setup_sources, monkeypatch):
    db, source = setup_sources
    source.connected.add("google_calendar")

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 15, 12, tzinfo=UTC)

    monkeypatch.setattr("app.services.assistant_sources.datetime", Frozen)
    source.handlers["google_calendar.event.list"] = lambda args: {
        "events": [{"id": "two" if args.get("page_token") else "one", "summary": "Meeting",
                    "description": "private discussion"}],
        "next_page_token": None if args.get("page_token") else "second", "has_more": not bool(args.get("page_token")),
    }
    result = source_named(collect_sources(db, session_id=17), "calendar.events")
    assert result["complete"] and set(result["items"]) == {"one", "two"}
    assert "2025-12-01" in result["scope"] and "2026-05-01" in result["scope"]
    assert source.calls[0][1]["time_min"] == source.calls[1][1]["time_min"]
    assert source.calls[0][1]["time_max"] == source.calls[1][1]["time_max"]
    assert "private discussion" not in str(result)


def test_outlook_merges_conversation_fragments_across_search_pages(setup_sources):
    db, source = setup_sources
    source.connected.add("outlook")
    source.handlers["email.search"] = lambda args: {
        "conversations": [{"conversation_id": "thread", "subject": "Hello", "message_count": 1,
                           "latest_timestamp": "2026-10-15T10:00:00Z" if args.get("page_token")
                           else "2026-10-16T10:00:00Z", "unread": bool(args.get("page_token")),
                           "snippet": "private body fragment", "has_attachment": False}],
        "next_page_token": None if args.get("page_token") else "next", "has_more": not bool(args.get("page_token")),
    }
    result = source_named(collect_sources(db, session_id=17), "email.outlook")
    assert result["complete"]
    assert result["items"]["thread"]["message_count"] == 2
    assert result["items"]["thread"]["unread"] is True
    assert result["items"]["thread"]["latest_timestamp"] == "2026-10-16T10:00:00Z"
    assert "private body fragment" not in str(result)
    assert result["removal_kind"] == "left_scope"


def test_replaced_account_has_different_source_key(setup_sources):
    db, source = setup_sources
    source.connected.add("notion")
    source.handlers["notion.todo.list"] = lambda args: {"todos": [], "next_cursor": None, "has_more": False}
    first = {key for key in collect_sources(db, session_id=17) if key.startswith("notion.todos:")}
    source.account_id = "replacement"
    second = {key for key in collect_sources(db, session_id=17) if key.startswith("notion.todos:")}
    assert first.isdisjoint(second)


def test_atlas_revision_drift_prevents_link_snapshot(setup_sources):
    db, source = setup_sources
    source.connected.add("atlas")
    source.handlers["atlas.records.search"] = lambda args: {"items": [{"id": "one", "revision": 1}], "next_cursor": None}
    source.handlers["atlas.records.get"] = lambda args: {"record": {"id": "one", "revision": 2}}
    result = source_named(collect_sources(db, session_id=17), "atlas.links")
    assert not result["complete"] and result["error"] == "stale_revision" and result["items"] == {}


def test_memory_snapshot_hashes_values_without_copying_them(setup_sources):
    db, _ = setup_sources
    memory = AgentMemoryService(db)
    fact = memory.create_fact({"key": "outside_fact", "value": "private value"})
    result = collect_sources(db, session_id=17)["memory"]
    first = result["items"][str(fact["id"])]
    assert result["complete"] and first["key"] == "outside_fact"
    assert first["category"] == "facts"
    assert "private value" not in str(result)
    memory.update_fact(fact["id"], {"value": "changed private value"})
    assert collect_sources(db, session_id=17)["memory"]["items"][str(fact["id"])]["content_hash"] != first["content_hash"]


def test_available_sources_rechecks_actual_policy_without_reading_source_content(setup_sources):
    db, source = setup_sources
    source.connected.update({"atlas", "notion"})
    before = available_sources(db, session_id=17)
    assert any(key.startswith("notion.todos:") for key in before)
    assert any(key.startswith("atlas.records:") for key in before)
    assert any(key.startswith("atlas.links:") for key in before)
    db.add(AgentPolicy(id="assistant", policy_json={"banned_functions": ["notion.todo.list", "atlas.records.get"]}))
    db.commit()
    after = available_sources(db, session_id=17)
    assert not any(key.startswith("notion.todos:") for key in after)
    assert not any(key.startswith("atlas.links:") for key in after)
    assert any(key.startswith("atlas.records:") for key in after)
    assert "memory" in after and source.calls == []


def test_available_sources_identity_changes_with_actual_connection_metadata(setup_sources):
    db, source = setup_sources
    connection = IntegrationConnection(provider="notion", is_default=True, secret_store_id="test",
        secret_reference="opaque", status="connected", account_login="test", account_id="first",
        configured_resource_id="first-todos", last_validated_at=datetime.now(UTC))
    db.add(connection)
    db.commit()
    source.connected.add("notion")
    source._connection = lambda provider: connection if provider == "notion" else None
    before = available_sources(db, session_id=17)
    connection.account_id, connection.configured_resource_id = "replacement", "replacement-todos"
    db.commit()
    after = available_sources(db, session_id=17)
    assert before - after and after - before
    assert source.calls == []
    source.connected.clear()
    assert available_sources(db, session_id=17) == {"memory"}


@pytest.mark.parametrize("change", ["identity", "ban", "disconnect"])
def test_source_changes_during_scan_discard_all_collected_items(setup_sources, change):
    db, source = setup_sources
    source.connected.add("notion")

    def read(args):
        if change == "identity":
            source.account_id = "changed-mid-scan"
        elif change == "ban":
            db.add(AgentPolicy(id="assistant", policy_json={"banned_functions": ["notion.todo.list"]}))
            db.commit()
        else:
            source.connected.clear()
        return {"todos": [{"id": "one", "title": "Todo"}], "next_cursor": None, "has_more": False}

    source.handlers["notion.todo.list"] = read
    result = source_named(collect_sources(db, session_id=17), "notion.todos")
    assert not result["complete"] and result["items"] == {}
    assert result["error"] in {"connection_unavailable", "agent_permission_denied"}


def test_archived_session_cannot_revalidate_or_collect_memory(setup_sources):
    db, source = setup_sources
    db.get(ActSession, 17).status = "archived"
    db.commit()
    assert available_sources(db, session_id=17) == set()
    assert not collect_sources(db, session_id=17)["memory"]["complete"]
    assert source.calls == []
