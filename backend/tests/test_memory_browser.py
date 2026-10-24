from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.memory import router
from app.services import act_workspace_service
from app.services.agent_memory_service import AgentMemoryService


@pytest.fixture
def memory_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(act_workspace_service, "ACT_ROOT", tmp_path / "agents")
    workspace = act_workspace_service.ensure_act_workspace()
    service = AgentMemoryService(workspace=workspace)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client, service


def archive_message(
    service: AgentMemoryService,
    agent: str,
    timestamp: datetime,
    source_id: int,
    role: str,
    text: str,
) -> None:
    source = "live" if source_id >= 100 else "turn"
    turn_id = source_id if source == "turn" else source_id - 100
    service._archive_message(
        agent=agent,
        source=source,
        source_id=source_id,
        role=role,
        text=text,
        timestamp=timestamp,
        session_id=1,
        turn_id=turn_id,
        cutoff=datetime(2026, 10, 1, tzinfo=UTC),
    )


def test_daily_summaries_are_paginated_newest_first_past_365_days_and_hide_receipt(memory_browser):
    client, service = memory_browser
    first_day = date(2025, 1, 1)
    for index in range(366):
        day = (first_day + timedelta(days=index)).isoformat()
        service.write_daily_summary(day, f"Summary for {day}", source_turn_id=index + 1)

    response = client.get("/memory/daily-summaries?offset=364&limit=2")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 366
    assert payload["offset"] == 364
    assert payload["limit"] == 2
    assert [item["day"] for item in payload["items"]] == ["2025-01-02", "2025-01-01"]
    assert payload["items"][0]["summary"] == "Summary for 2025-01-02"
    assert payload["items"][0]["source_turn_id"] == 2
    assert "<!-- eidolon-daily-summary" not in response.text


def test_chat_history_days_and_messages_page_in_utc_and_filter_by_agent(memory_browser):
    client, service = memory_browser
    archive_message(service, "assistant", datetime(2026, 10, 18, 23, 30, tzinfo=UTC), 9, "user", "Oct 18")
    archive_message(service, "assistant", datetime(2026, 10, 19, 1, tzinfo=UTC), 10, "user", "Assistant first")
    archive_message(service, "assistant", datetime(2026, 10, 19, 2, tzinfo=UTC), 10, "assistant", "Assistant second")
    archive_message(service, "act", datetime(2026, 10, 19, 1, 30, tzinfo=UTC), 20, "user", "Act message")

    first_days = client.get("/memory/chat-history/days?offset=0&limit=1").json()
    next_days = client.get("/memory/chat-history/days?offset=1&limit=1").json()
    assert first_days == {"items": ["2026-10-19"], "total": 2, "offset": 0, "limit": 1}
    assert next_days["items"] == ["2026-10-18"]

    page = client.get("/memory/chat-history?day=2026-10-19&agent=assistant&offset=1&limit=1")
    assert page.status_code == 200
    assert page.json() == {
        "items": [{
            "agent": "assistant",
            "role": "assistant",
            "text": "Assistant second",
            "timestamp": "2026-10-19T02:00:00+00:00",
            "session_id": 1,
            "turn_id": 10,
            "source": "turn",
            "source_id": 10,
        }],
        "total": 2,
        "offset": 1,
        "limit": 1,
    }

    all_agents = client.get("/memory/chat-history?day=2026-10-19&limit=100").json()
    assert all_agents["total"] == 3
    assert [message["agent"] for message in all_agents["items"]] == ["assistant", "act", "assistant"]


def test_memory_browser_validates_dates_agents_and_page_bounds(memory_browser):
    client, _service = memory_browser

    assert client.get("/memory/chat-history?day=2026-02-30").status_code == 422
    assert client.get("/memory/chat-history?day=not-a-date").status_code == 422
    assert client.get("/memory/chat-history?day=2026-10-19&agent=unknown").status_code == 422
    assert client.get("/memory/daily-summaries?limit=0").status_code == 422
    assert client.get("/memory/chat-history/days?offset=-1").status_code == 422
    assert client.get("/memory/chat-history?day=2026-10-19&limit=101").status_code == 422


def test_memory_browsing_does_not_synchronize_or_change_archives(memory_browser, monkeypatch):
    client, service = memory_browser
    archive_message(service, "observer", datetime(2026, 10, 19, 8, tzinfo=UTC), 101, "assistant", "Retained text")
    files = sorted(service.history.glob("*/*.json"))
    before = {path: path.read_bytes() for path in files}

    def unexpected_sync(*_args, **_kwargs):
        pytest.fail("Browsing must not synchronize or mutate chat archives")

    monkeypatch.setattr(AgentMemoryService, "sync_chat_history", unexpected_sync)
    monkeypatch.setattr(AgentMemoryService, "migrate_legacy", unexpected_sync)

    assert client.get("/memory/chat-history/days").status_code == 200
    assert client.get("/memory/chat-history?day=2026-10-19").status_code == 200
    assert {path: path.read_bytes() for path in files} == before
