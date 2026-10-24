from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.execution.context_factory import InvocationContextFactory
from app.execution.executor import InvocationExecutor
from app.integrations.valence import NATIVE_INPUT
from app.models import ActSession, ActTurn, IntegrationAuditRecord, McpAuditRecord
from app.schemas.agents import AgentPolicyUpdate
from app.services.agent_policy_service import AgentPolicyService
from app.services.function_catalog_service import FunctionCatalogService
from app.services.github_provider import IntegrationProviderError
from app.services.integration_service import IntegrationError, IntegrationService
from app.services.mcp_function_service import McpFunctionError, McpFunctionService
from tests.test_integration_service import (
    authorize,
    caller_context,
    create_installed_skill,
    integration_requirement,
)

VALENCE_OPERATIONS = ["valence.describe", "valence.utility.calculate"]


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


class FakeValenceTransport:
    def __init__(self) -> None:
        self.is_available = True
        self.calls: list[tuple[str, dict]] = []
        self.error_type: str | None = None

    def available(self) -> bool:
        return self.is_available

    def execute(self, operation, input_json: dict) -> dict:
        self.calls.append((operation.id, input_json))
        if self.error_type:
            raise IntegrationProviderError(self.error_type, "private provider details")
        if operation.id == "valence.describe":
            return {"description": {"engine_version": "8", "section": input_json.get("section", "overview")}}
        return _calculate_output(input_json)


def _valid_calculation_input() -> dict:
    feature_keys = NATIVE_INPUT["action"]["properties"]["features"]["required"]
    return {
        "person_id": "person-1",
        "actions": [
            {
                "id": "candidate-1",
                "action": {
                    "title": "Encoded candidate",
                    "duration_minutes": 60,
                    "net_money_cost": 0,
                    "features": dict.fromkeys(feature_keys, 0),
                },
            }
        ],
        "state": {"date": "2026-10-16"},
    }


def _calculate_output(input_json: dict) -> dict:
    return {
        "engine_version": "8",
        "person_id": input_json["person_id"],
        "model_version_id": "model-version-1",
        "model_version": 3,
        "model_snapshot": {"id": "model-1"},
        "converted_from_legacy": False,
        "state": input_json["state"],
        "horizon_hours": input_json.get("horizon_hours", 168),
        "results": [
            {
                "action_id": input_json["actions"][0]["id"],
                "action_snapshot": input_json["actions"][0]["action"],
                "rank": 1,
                "total_utility": 1.23456789,
                "utility_per_action_hour": 1.23456789,
            }
        ],
    }


def _skill(db: Session, tmp_path, operations: list[str]):
    skill, manifest = create_installed_skill(
        db,
        tmp_path,
        requirement=integration_requirement(provider="valence", operations=operations),
    )
    return skill, manifest


def test_valence_describe_and_calculate_use_authorized_integration_path_and_audit(
    db: Session,
    tmp_path,
) -> None:
    transport = FakeValenceTransport()
    service = IntegrationService(db, project_root=tmp_path, valence=transport)
    skill, manifest = _skill(db, tmp_path, VALENCE_OPERATIONS)
    authorize(service, skill, manifest)

    description = service.execute_context(
        caller_context(skill), "valence.describe", {}
    ).output
    calculation_input = _valid_calculation_input()
    calculation = service.execute_context(
        caller_context(skill), "valence.utility.calculate", calculation_input
    ).output

    assert description == {"description": {"engine_version": "8", "section": "overview"}}
    assert calculation["results"][0]["total_utility"] == 1.23456789
    assert transport.calls == [
        ("valence.describe", {}),
        ("valence.utility.calculate", calculation_input),
    ]
    audits = db.scalars(select(IntegrationAuditRecord).order_by(IntegrationAuditRecord.id)).all()
    assert [(audit.operation_id, audit.status, audit.resource) for audit in audits] == [
        ("valence.describe", "succeeded", None),
        ("valence.utility.calculate", "succeeded", "person-1"),
    ]

    transport.error_type = "provider_unavailable"
    with pytest.raises(IntegrationError) as failed:
        service.execute_context(caller_context(skill), "valence.describe", {})
    assert failed.value.error_type == "provider_unavailable"
    failure_audit = db.scalars(select(IntegrationAuditRecord).order_by(IntegrationAuditRecord.id)).all()[-1]
    assert failure_audit.status == "failed"
    assert failure_audit.error_type == "provider_unavailable"
    assert "private provider details" not in str(failed.value)


@pytest.mark.parametrize("malformation", ["missing_feature", "noncanonical_feature"])
def test_calculate_schema_rejects_incomplete_or_noncanonical_features_before_transport(
    db: Session,
    tmp_path,
    malformation: str,
) -> None:
    transport = FakeValenceTransport()
    service = IntegrationService(db, project_root=tmp_path, valence=transport)
    skill, manifest = _skill(db, tmp_path, ["valence.utility.calculate"])
    authorize(service, skill, manifest)
    payload = _valid_calculation_input()
    features = payload["actions"][0]["action"]["features"]
    if malformation == "missing_feature":
        del features["social_exposure"]
    else:
        del features["social_exposure"]
        features["social_interaction"] = 0

    with pytest.raises(IntegrationError) as invalid:
        service.execute_context(caller_context(skill), "valence.utility.calculate", payload)

    assert invalid.value.error_type == "invalid_input"
    assert transport.calls == []
    audit = db.scalar(select(IntegrationAuditRecord))
    assert audit is not None
    assert (audit.status, audit.error_type) == ("failed", "invalid_input")


def test_undeclared_skill_operation_and_unavailable_provider_are_denied_before_transport(
    db: Session,
    tmp_path,
) -> None:
    transport = FakeValenceTransport()
    service = IntegrationService(db, project_root=tmp_path, valence=transport)
    skill, manifest = _skill(db, tmp_path, ["valence.describe"])
    authorize(service, skill, manifest)

    with pytest.raises(IntegrationError) as undeclared:
        service.execute_context(
            caller_context(skill), "valence.utility.calculate", _valid_calculation_input()
        )
    assert undeclared.value.error_type == "operation_undeclared"
    assert transport.calls == []

    transport.is_available = False
    with pytest.raises(IntegrationError) as unavailable:
        service.execute_context(caller_context(skill), "valence.describe", {})
    assert unavailable.value.error_type == "connection_unavailable"
    assert transport.calls == []

    audits = db.scalars(select(IntegrationAuditRecord).order_by(IntegrationAuditRecord.id)).all()
    assert [(audit.status, audit.error_type) for audit in audits] == [
        ("failed", "operation_undeclared"),
        ("failed", "connection_unavailable"),
    ]


def test_assistant_discovers_and_calls_valence_then_ban_is_enforced_and_audited(
    db: Session,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeValenceTransport()
    integration_service = IntegrationService(db, project_root=tmp_path, valence=transport)
    monkeypatch.setattr(
        "app.services.function_catalog_service.build_default_integration_service",
        lambda _db: SimpleNamespace(
            operation_available=lambda operation_id: operation_id in VALENCE_OPERATIONS
        ),
    )
    original_list_entries = FunctionCatalogService.list_entries

    def list_entries(self, *args, **kwargs):
        rooted = FunctionCatalogService(self.db, project_root=tmp_path)
        return original_list_entries(rooted, *args, **kwargs)

    monkeypatch.setattr(FunctionCatalogService, "list_entries", list_entries)
    FunctionCatalogService(db, project_root=tmp_path).refresh()
    entries = FunctionCatalogService(db, project_root=tmp_path).list_entries()
    utility_entry = next(item for item in entries if item["id"] == "valence.utility.calculate")
    assert utility_entry["availability"] == "available"
    assert utility_entry["risk_level"] == "low"
    assert utility_entry["invocation"]["effects"] == ["read"]
    assert utility_entry["requires_invocation_approval"] is False
    assert utility_entry["mcp_read_only"] is True
    assert AgentPolicyService(db).decision("assistant", utility_entry) == (True, "Allowed by default policy")

    session = ActSession(agent_id="assistant", codex_thread_id="valence-integration-test")
    db.add(session)
    db.flush()
    turn = ActTurn(session_id=session.id, user_message="Calculate utility", status="running")
    db.add(turn)
    db.commit()
    policy = AgentPolicyService(db)
    credential = policy.issue("assistant", session.id)
    mcp = McpFunctionService(
        db,
        project_root=tmp_path,
        agent_token=credential,
        executor=InvocationExecutor(
            db,
            project_root=tmp_path,
            integration_service=integration_service,
        ),
    )
    tool_name = McpFunctionService.tool_name("integration", "valence.utility.calculate")
    assert tool_name in {tool.name for tool in mcp.list_tools()}
    context = InvocationContextFactory(db, project_root=tmp_path).from_agent_credential(credential)
    assert context.agent_id == "assistant"
    result = mcp.invoke(tool_name, _valid_calculation_input())
    assert result.output["results"][0]["total_utility"] == 1.23456789
    assert transport.calls == [("valence.utility.calculate", _valid_calculation_input())]

    policy.update(
        "assistant",
        AgentPolicyUpdate(banned_functions=["valence.utility.calculate"]),
    )
    assert tool_name not in {tool.name for tool in mcp.list_tools()}
    with pytest.raises(McpFunctionError) as denied:
        mcp.invoke(tool_name, _valid_calculation_input())
    assert denied.value.error_type == "agent_permission_denied"
    assert len(transport.calls) == 1
    audit = db.scalar(
        select(McpAuditRecord)
        .where(McpAuditRecord.function_id == "valence.utility.calculate")
        .order_by(McpAuditRecord.id.desc())
    )
    assert audit is not None
    assert (audit.status, audit.error_type) == ("failed", "agent_permission_denied")
