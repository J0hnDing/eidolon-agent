from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.routers.agents import router
from app.services.function_catalog_service import FunctionCatalogService


def test_agent_api_session_identity_and_policy_contract(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(FunctionCatalogService, "list_entries", lambda _self: [])

    def database():
        with factory() as db:
            yield db

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = database
    with TestClient(app) as client:
        agents = client.get("/agents").json()
        assert [agent["id"] for agent in agents] == ["act", "observer", "assistant"]
        assert agents[1]["permissions"]["web_search"] is False
        created = client.post("/agents/observer/sessions", json={"origin": "web"})
        assert created.status_code == 201
        session = created.json()
        assert session["agent_id"] == "observer"
        assert session["turns"] == []
        assert client.get(f'/agents/assistant/sessions/{session["id"]}').status_code == 404
        policy = {**agents[1]["policy"], "max_risk": "low"}
        response = client.put("/agents/observer/policy", json=policy)
        assert response.status_code == 200
        assert response.json()["policy"]["max_risk"] == "low"
    engine.dispose()
