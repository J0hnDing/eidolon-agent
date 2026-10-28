import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import get_db
from app.routers.agents import router
from app.schemas.codex_routing import CodexRoutingSettingsRead, InvocationChoice
from app.services.act_app_server_service import render_agent_instructions
from app.services.codex_routing_service import CodexRoutingError, CodexRoutingService


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: None
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("agent_id", ["act", "observer", "assistant"])
def test_instructions_are_the_exact_runtime_base_instructions(client, agent_id):
    response = client.get(f"/agents/{agent_id}/instructions")
    assert response.status_code == 200
    assert response.json() == {"agent_id": agent_id, "instructions": render_agent_instructions(agent_id, ())}


def test_settings_reject_unknown_agents_and_instruction_writes(client):
    assert client.get("/agents/unknown/instructions").status_code == 404
    assert client.put("/agents/unknown/model", json={}).status_code == 404
    assert client.put("/agents/act/instructions", json={"instructions": "replace"}).status_code == 405


def test_model_alias_uses_existing_routing_service_and_surfaces_validation(client, monkeypatch):
    calls = []

    def update(_service, agent_id, choice):
        calls.append((agent_id, choice))
        return CodexRoutingSettingsRead(assessment=choice)

    monkeypatch.setattr(CodexRoutingService, "update_agent_model", update)
    response = client.put("/agents/assistant/model", json={"model": "smart", "reasoning_effort": "high"})
    assert response.status_code == 200
    assert calls == [("assistant", InvocationChoice(model="smart", reasoning_effort="high"))]
    assert response.json()["assessment"] == {"model": "smart", "reasoning_effort": "high"}

    def reject(*_args):
        raise CodexRoutingError("Unavailable model")

    monkeypatch.setattr(CodexRoutingService, "update_agent_model", reject)
    response = client.put("/agents/act/model", json={"model": "missing"})
    assert response.status_code == 409
    assert response.json()["detail"] == "Unavailable model"
    assert client.put("/agents/act/model", json={"model": ""}).status_code == 422
