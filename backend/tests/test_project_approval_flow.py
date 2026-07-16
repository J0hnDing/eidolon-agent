from collections.abc import Generator

import pytest
from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import AgentRun, ApprovalRequest, Skill, SkillGenerationRequest
from app.routers.permission_requests import approve_permission_request
from app.routers.skill_generation_requests import get_project_conversation_state
from app.services.agent_workflow_service import AgentWorkflowService


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def create_project_state(db: Session) -> tuple[SkillGenerationRequest, ApprovalRequest, AgentRun]:
    generation = SkillGenerationRequest(
        user_message="Build a counter",
        proposed_skill_name="persistent_counter",
        proposed_display_name="Persistent Counter",
        plan_json={"frontend_conversation_id": "chat-counter"},
        requested_permissions_json={},
        requested_dependencies_json=[],
        requested_network_domains_json=[],
        risk_level="low",
        status="awaiting_approval",
    )
    db.add(generation)
    db.flush()
    approval = ApprovalRequest(
        generation_request_id=generation.id,
        request_scope="build_time",
        request_type="generation",
        risk_level="low",
        requested_permissions_json={},
        requested_dependencies_json=[],
        requested_network_domains_json=[],
        requested_filesystem_json={},
        reason_json={},
        reason="Build",
        user_explanation="Build",
        status="pending",
    )
    run = AgentRun(
        run_type="build_skill",
        status="waiting_for_approval",
        generation_request_id=generation.id,
        user_request=generation.user_message,
    )
    db.add_all([approval, run])
    db.commit()
    db.refresh(generation)
    db.refresh(approval)
    db.refresh(run)
    return generation, approval, run


def test_conversation_state_recovers_build_and_runtime_approvals(db_session: Session) -> None:
    generation, approval, run = create_project_state(db_session)
    skill = Skill(
        name="persistent_counter",
        description="Counter",
        runtime="web_app",
        status="proposed",
        risk_level="low",
        manifest_path="skills/proposed/persistent_counter/manifest.json",
        enabled=False,
    )
    db_session.add(skill)
    db_session.flush()
    generation.proposed_skill_id = skill.id
    run.skill_id = skill.id
    runtime_approval = ApprovalRequest(
        skill_id=skill.id,
        request_scope="runtime",
        request_type="install",
        risk_level="low",
        requested_permissions_json={},
        requested_dependencies_json=[],
        requested_network_domains_json=[],
        requested_filesystem_json={},
        reason_json={},
        reason="Runtime",
        user_explanation="Runtime",
        status="pending",
    )
    db_session.add(runtime_approval)
    db_session.commit()

    state = get_project_conversation_state("chat-counter", db_session)

    assert state is not None
    assert state.generation_request.id == generation.id
    assert state.permission_request is not None
    assert state.permission_request.id == approval.id
    assert state.proposed_skill is not None
    assert state.proposed_skill.id == skill.id
    assert state.agent_run is not None
    assert state.agent_run.id == run.id
    assert state.runtime_permission_request is not None
    assert state.runtime_permission_request.id == runtime_approval.id
    assert state.needs_polling is True


def test_generic_build_approval_resumes_linked_agent_run_automatically(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation, approval, run = create_project_state(db_session)
    resumed: list[int] = []

    def fake_resume(self: AgentWorkflowService, agent_run: AgentRun) -> AgentRun:
        resumed.append(agent_run.id)
        agent_run.status = "running"
        self.db.commit()
        return agent_run

    monkeypatch.setattr(AgentWorkflowService, "resume_run", fake_resume)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/permission-requests/{approval.id}/approve",
            "headers": [],
        }
    )

    updated = approve_permission_request(approval.id, request, db=db_session)

    assert updated.status == "approved"
    assert generation.status == "approved"
    assert resumed == [run.id]
