import hashlib
import json
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.execution.context import InvocationContext
from app.execution.context_factory import InvocationContextFactory
from app.execution.executor import InvocationExecutor
from app.execution.types import InvocationOutcome
from app.models import (
    ApprovalRequest,
    InvocationApproval,
    Skill,
    SkillRun,
    SkillSchedule,
    SkillVersion,
)
from app.schemas.manifest import SkillManifest
from app.services.function_catalog_service import FunctionCatalogService
from app.services.function_registry_service import (
    FunctionRegistryError,
    FunctionRegistryService,
)
from app.services.invocation_approval_contract import INVOCATION_APPROVAL_DESCRIPTION_SUFFIX
from app.services.invocation_approval_service import InvocationApprovalService
from app.services.permission_service import PermissionService
from app.services.proposed_skill_service import ProposedSkillService


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


class FakeRunner:
    def __init__(self, db: Session, output: dict[str, Any] | None = None) -> None:
        self.db = db
        self.output = output or {"result": "ok"}
        self.calls: list[dict[str, Any]] = []

    def run(self, skill_id: int, skill_dir: Path, input_json: dict[str, Any], context=None) -> SkillRun:
        self.calls.append({"skill_id": skill_id, "skill_dir": skill_dir, "input": input_json, "context": context})
        run = SkillRun(
            skill_id=skill_id,
            version_id=context.version_id if context else None,
            status="succeeded",
            input_json=input_json,
            output_json=self.output,
            started_at=datetime.now(UTC),
            ended_at=datetime.now(UTC),
            exit_code=0,
            invocation_source=context.invocation_source if context else "internal",
            caller_skill_id=context.caller_skill_id if context else None,
            caller_version_id=context.caller_version_id if context else None,
            parent_run_id=context.parent_run_id if context else None,
            initiating_action=context.initiating_action if context else None,
            function_capability_token_hash=context.capability_token_hash if context else None,
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run


def runtime_context(caller: Skill, *, run_id: int | None = None) -> InvocationContext:
    return InvocationContext(
        principal_kind="skill",
        origin="skill_runtime",
        caller_skill_id=caller.id,
        caller_version_id=caller.active_version_id,
        caller_run_id=run_id,
        caller_runtime=caller.runtime,
        initiating_action="test_call",
    )


def invoke_registry(
    registry: FunctionRegistryService,
    target: Skill,
    input_json: dict[str, Any],
    context: InvocationContext,
):
    error = registry.caller_authorization_error(target, context)
    if error is not None:
        return registry.blocked_run_for_context(target, input_json, error, context)
    return registry.execute_resolved(target, input_json, context)


def make_function(
    db: Session,
    project_root: Path,
    name: str,
    *,
    requirements: list[str] | None = None,
    network: list[str] | None = None,
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
    requires_invocation_approval: bool = False,
) -> Skill:
    skill_dir = project_root / "skills" / "installed" / name / "versions" / "v1"
    (skill_dir / "tests").mkdir(parents=True)
    permissions = {
        "network": network or [],
        "filesystem_read": [],
        "filesystem_write": ["./cache"],
        "secrets": [],
        "shell": False,
    }
    manifest = {
        "manifest_version": 1,
        "name": name,
        "description": f"{name} description",
        "runtime": "function",
        "entrypoint": "skill.py",
        "instructions_path": None,
        "input_schema": input_schema
        or {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        "output_schema": output_schema
        or {
            "type": "object",
            "properties": {"result": {"type": "string"}},
            "required": ["result"],
            "additionalProperties": False,
        },
        "requires_invocation_approval": requires_invocation_approval,
        "function_requirements": requirements or [],
        "dependencies": [],
        "permissions": permissions,
        "schedule": None,
    }
    (skill_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (skill_dir / "skill.py").write_text("print('{}')\n", encoding="utf-8")
    (skill_dir / "tests" / "test_skill.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    skill = Skill(
        name=name,
        description=manifest["description"],
        runtime="function",
        status="installed",
        risk_level="medium" if network else "low",
        manifest_path=(skill_dir / "manifest.json").relative_to(project_root).as_posix(),
        installed_path=skill_dir.relative_to(project_root).as_posix(),
        input_schema_json=manifest["input_schema"],
        output_schema_json=manifest["output_schema"],
        function_requirements_json=requirements or [],
        enabled=True,
    )
    db.add(skill)
    db.flush()
    version = SkillVersion(
        skill_id=skill.id,
        version="v1",
        status="active",
        folder_path=skill.installed_path,
        code_snapshot_path=skill.installed_path,
        manifest_json=manifest,
        permission_fingerprint="test",
        test_status="passed",
        validation_status="passed",
    )
    db.add(version)
    db.flush()
    skill.active_version_id = version.id
    db.commit()
    db.refresh(skill)
    request = PermissionService(db, project_root=project_root).create_runtime_request(skill)
    PermissionService(db, project_root=project_root).approve_request(request)
    return skill


def service(db: Session, project_root: Path, runner: FakeRunner) -> FunctionRegistryService:
    return FunctionRegistryService(db, project_root=project_root, runner_factory=lambda _db: runner)


def test_registry_exposes_backend_validated_contract(tmp_path: Path, db_session: Session) -> None:
    target = make_function(db_session, tmp_path, "normalize_text")
    registry = FunctionRegistryService(db_session, project_root=tmp_path)

    contracts = registry.list_contracts()

    assert len(contracts) == 1
    assert contracts[0].skill_id == target.id
    assert contracts[0].active_version == "v1"
    assert contracts[0].input_schema["required"] == ["value"]
    assert contracts[0].risk_level == "low"
    assert contracts[0].availability == "available"
    assert "entrypoint" not in contracts[0].model_dump()


def test_invocation_approval_projects_contract_and_defers_execution(
    tmp_path: Path,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = make_function(
        db_session,
        tmp_path,
        "send_sensitive_action",
        requires_invocation_approval=True,
    )
    runner = FakeRunner(db_session)

    class FakeTelegram:
        def approval_available(self) -> bool:
            return True

        def deliver_invocation_approval(self, approval: InvocationApproval) -> None:
            approval.telegram_delivery_status = "delivered"

    monkeypatch.setattr(
        InvocationApprovalService,
        "_telegram_service",
        lambda _self: FakeTelegram(),
    )
    registry = service(db_session, tmp_path, runner)
    contract = registry.contract_for_skill(target)

    assert contract.description == f"send_sensitive_action description {INVOCATION_APPROVAL_DESCRIPTION_SUFFIX}"
    assert contract.risk_level == "high"
    assert contract.requires_invocation_approval is True
    assert contract.input_schema["required"] == ["value", "reason_to_call"]
    assert contract.output_schema["properties"]["status"]["const"] == "pending_approval"
    approval_fingerprint = registry.target_contract_fingerprint(target)
    manifest_path = tmp_path / target.manifest_path
    manifest_json = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_json["requires_invocation_approval"] = False
    manifest_path.write_text(json.dumps(manifest_json), encoding="utf-8")
    assert registry.target_contract_fingerprint(target) != approval_fingerprint
    manifest_json["requires_invocation_approval"] = True
    manifest_path.write_text(json.dumps(manifest_json), encoding="utf-8")

    approval = registry.execute_resolved(
        target,
        {"value": "execute later", "reason_to_call": "The user requested it."},
        InvocationContext(
            principal_kind="user",
            origin="http",
            initiating_action="direct_user",
        ),
    )

    assert isinstance(approval, InvocationApproval)
    assert approval.input_json == {"value": "execute later"}
    assert approval.reason_to_call == "The user requested it."
    assert approval.decision_status == "pending"
    assert approval.dispatch_metadata_json["invocation_context_v1"] == {
        "principal_kind": "user",
        "origin": "http",
        "initiating_action": "direct_user",
    }
    assert runner.calls == []

    monkeypatch.setattr(
        InvocationExecutor,
        "execute_approved",
        lambda _self, _approval: InvocationOutcome(
            status="succeeded",
            output={"result": "ok"},
        ),
    )
    decided = InvocationApprovalService(db_session, project_root=tmp_path).approve(
        approval.id,
        decided_via="local",
        decided_by="local_user",
    )
    assert decided.decision_status == "approved"
    assert decided.execution_status == "succeeded"
    assert decided.result_json == {"result": "ok"}


def test_manifest_rejects_invalid_invocation_approval_contracts() -> None:
    base = {
        "name": "approval_contract",
        "description": "Approval contract",
        "runtime": "service",
        "entrypoint": "skill.py",
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": {"type": "object", "properties": {}},
        "permissions": {},
        "schedule": {
            "type": "daily",
            "time": "09:00",
            "timezone": "America/Toronto",
            "input": {},
        },
        "requires_invocation_approval": True,
    }
    with pytest.raises(PydanticValidationError, match="supported only for function"):
        SkillManifest.model_validate(base)

    base.update(
        {
            "runtime": "function",
            "schedule": None,
            "input_schema": {
                "type": "object",
                "properties": {"reason_to_call": {"type": "string"}},
            },
        }
    )
    with pytest.raises(PydanticValidationError, match="reason_to_call is reserved"):
        SkillManifest.model_validate(base)


def test_unified_catalog_persists_categories_states_and_user_lifecycle(
    tmp_path: Path,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection_state = {"github": False, "notion": False}
    monkeypatch.setattr(
        "app.services.function_catalog_service.build_default_integration_service",
        lambda _db: SimpleNamespace(
            connection_status=lambda: SimpleNamespace(connected=connection_state["github"]),
            provider_connected=lambda provider: connection_state.get(provider, False),
            operation_available=lambda operation_id: connection_state.get(
                "notion" if operation_id.startswith("notion.") else "github",
                False,
            ),
        ),
    )
    target = make_function(db_session, tmp_path, "normalize_text")
    make_function(db_session, tmp_path, "network_lookup", network=["example.com"])
    catalog = FunctionCatalogService(db_session, project_root=tmp_path)

    catalog.register_user_function(target)
    entries = {entry["id"]: entry for entry in catalog.list_entries(refresh=False)}

    assert (tmp_path / "runtime" / "function_catalog.json").is_file()
    assert entries["backend.codex.call"]["category"] == "backend_core"
    assert entries["backend.codex.call"]["availability"] == "available"
    assert entries["backend.codex.call"]["mcp_exposed"] is False
    assert "backend.notion.todo.cleanup_done" not in entries
    assert entries["normalize_text"]["category"] == "user"
    assert entries["normalize_text"]["availability"] == "available"
    assert entries["normalize_text"]["mcp_exposed"] is True
    assert entries["normalize_text"]["mcp_read_only"] is False
    assert entries["normalize_text"]["mcp_open_world"] is False
    assert entries["network_lookup"]["mcp_open_world"] is True
    assert entries["github.repository.get"]["category"] == "integration"
    assert entries["github.repository.get"]["availability"] == "unavailable"
    assert entries["github.repository.get"]["availability_reasons"] == [
        "GitHub connection is not configured"
    ]
    assert entries["notion.todo.list"]["availability"] == "unavailable"
    assert entries["notion.todo.list"]["availability_reasons"] == [
        "Notion connection is not configured"
    ]
    assert entries["notion.todo.create"]["risk_level"] == "medium"
    assert entries["notion.todo.list"]["mcp_read_only"] is True
    assert entries["notion.todo.list"]["mcp_open_world"] is True
    assert entries["notion.todo.delete"]["mcp_destructive"] is True
    assert entries["notion.report.list"]["mcp_read_only"] is True
    assert entries["notion.report.delete"]["mcp_destructive"] is True
    assert entries["notion.todo.update"]["mcp_destructive"] is False
    assert entries["notion.todo.create"]["invocation"]["risk"] == "medium"
    assert "integration_test_adapter.DeterministicFakeIntegrationAdapter" in entries[
        "notion.todo.create"
    ]["invocation"]["test_adapter"]
    available_index = {entry["id"]: entry for entry in catalog.available_index()}
    assert available_index["backend.codex.call"]["risk_level"] == "low"
    assert "input_schema" not in available_index["backend.codex.call"]
    assert "github.repository.get" not in available_index

    connection_state["github"] = True
    connection_state["notion"] = True
    catalog.refresh()
    connected = {entry["id"]: entry for entry in catalog.list_entries()}
    assert connected["github.repository.get"]["availability"] == "available"
    assert connected["notion.todo.list"]["availability"] == "available"
    assert "github.repository.get" in {
        entry["id"] for entry in catalog.available_index()
    }

    target.enabled = False
    db_session.commit()
    catalog.refresh()
    disabled = {entry["id"]: entry for entry in catalog.list_entries()}
    assert disabled["normalize_text"]["availability"] == "disabled"

    ProposedSkillService(db_session, project_root=tmp_path).delete_skill(target)
    remaining_ids = {entry["id"] for entry in catalog.list_entries(refresh=False)}
    assert "normalize_text" not in remaining_ids


def test_catalog_list_defaults_to_existing_projection(
    tmp_path: Path,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = FunctionCatalogService(db_session, project_root=tmp_path)
    catalog.refresh()
    monkeypatch.setattr(
        catalog,
        "refresh",
        lambda: pytest.fail("An existing catalog projection must not refresh during a list"),
    )

    assert catalog.list_entries()


def test_declared_low_risk_function_invokes_without_caller_approval(
    tmp_path: Path,
    db_session: Session,
) -> None:
    target = make_function(db_session, tmp_path, "normalize_text")
    caller = make_function(
        db_session,
        tmp_path,
        "caller",
        requirements=[target.name],
    )
    runner = FakeRunner(db_session)
    registry = service(db_session, tmp_path, runner)

    run = invoke_registry(
        registry,
        target,
        {"value": "Hello"},
        runtime_context(caller),
    )

    assert run.status == "succeeded"
    assert run.caller_skill_id == caller.id
    assert run.caller_version_id == caller.active_version_id
    assert run.version_id == target.active_version_id
    assert runner.calls
    assert (
        db_session.scalar(
            select(ApprovalRequest)
            .where(ApprovalRequest.skill_id == caller.id)
            .where(ApprovalRequest.request_type == "function_access")
        )
        is None
    )


def test_codex_mcp_direct_call_keeps_run_history_lock_context_and_nested_capability(
    tmp_path: Path,
    db_session: Session,
) -> None:
    target = make_function(db_session, tmp_path, "normalize_text")
    runner = FakeRunner(db_session)
    registry = service(db_session, tmp_path, runner)

    run = registry.execute_resolved(
        target,
        {"value": "Hello"},
        InvocationContext(
            principal_kind="user",
            origin="codex_mcp",
            initiating_action="codex_mcp",
        ),
    )

    assert run.status == "succeeded"
    assert run.invocation_source == "codex_mcp"
    assert run.initiating_action == "codex_mcp"
    assert runner.calls[0]["context"].capability_token is not None
    assert runner.calls[0]["context"].capability_token_hash is not None


def test_undeclared_function_call_is_blocked_before_execution(tmp_path: Path, db_session: Session) -> None:
    target = make_function(db_session, tmp_path, "normalize_text")
    caller = make_function(db_session, tmp_path, "caller")
    runner = FakeRunner(db_session)

    registry = service(db_session, tmp_path, runner)
    run = invoke_registry(
        registry,
        target,
        {"value": "Hello"},
        runtime_context(caller),
    )

    assert run.status == "blocked"
    assert "did not declare" in run.error_message
    assert runner.calls == []


def test_medium_risk_relationship_requires_specific_approval(tmp_path: Path, db_session: Session) -> None:
    target = make_function(db_session, tmp_path, "network_lookup", network=["example.com"])
    caller = make_function(
        db_session,
        tmp_path,
        "caller",
        requirements=[target.name],
    )
    runner = FakeRunner(db_session)
    registry = service(db_session, tmp_path, runner)
    review = registry.review_requirements(caller, create_requests=True)

    assert review[0].approval_required is True
    assert review[0].access_state == "pending"
    blocked = invoke_registry(
        registry,
        target,
        {"value": "Hello"},
        runtime_context(caller),
    )
    assert blocked.status == "blocked"
    request = db_session.get(ApprovalRequest, review[0].approval_request_id)
    PermissionService(db_session, project_root=tmp_path).approve_request(request)

    allowed = invoke_registry(
        registry,
        target,
        {"value": "Hello"},
        runtime_context(caller),
    )
    assert allowed.status == "succeeded"
    assert runner.calls[-1]["context"].caller_skill_id == caller.id


def test_changed_target_permission_contract_makes_approval_stale(
    tmp_path: Path,
    db_session: Session,
) -> None:
    target = make_function(db_session, tmp_path, "network_lookup", network=["example.com"])
    caller = make_function(
        db_session,
        tmp_path,
        "caller",
        requirements=[target.name],
    )
    runner = FakeRunner(db_session)
    registry = service(db_session, tmp_path, runner)
    review = registry.review_requirements(caller, create_requests=True)
    request = db_session.get(ApprovalRequest, review[0].approval_request_id)
    PermissionService(db_session, project_root=tmp_path).approve_request(request)
    manifest_path = tmp_path / target.manifest_path
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["permissions"]["network"].append("api.example.com")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert registry.list_contracts(caller)[1].access_state in {"stale", "not_declared"}
    run = invoke_registry(
        registry,
        target,
        {"value": "Hello"},
        runtime_context(caller),
    )
    assert run.status == "blocked"
    assert run.error_message == "Runtime permissions do not match the current manifest"


def test_incompatible_input_is_blocked_and_output_contract_is_enforced(
    tmp_path: Path,
    db_session: Session,
) -> None:
    target = make_function(db_session, tmp_path, "normalize_text")
    caller = make_function(
        db_session,
        tmp_path,
        "caller",
        requirements=[target.name],
    )
    runner = FakeRunner(db_session, output={"unexpected": True})
    registry = service(db_session, tmp_path, runner)

    bad_input = invoke_registry(
        registry,
        target,
        {"value": 123},
        runtime_context(caller),
    )
    assert bad_input.status == "blocked"
    assert "input JSON is incompatible" in bad_input.error_message
    assert runner.calls == []

    bad_output = invoke_registry(
        registry,
        target,
        {"value": "Hello"},
        runtime_context(caller),
    )
    assert bad_output.status == "failed"
    assert "output JSON is incompatible" in bad_output.error_message


def test_disabled_target_is_visible_but_unavailable(tmp_path: Path, db_session: Session) -> None:
    target = make_function(db_session, tmp_path, "normalize_text")
    target.enabled = False
    db_session.commit()

    contract = FunctionRegistryService(db_session, project_root=tmp_path).list_contracts()[0]

    assert contract.availability == "disabled"
    assert contract.availability_reasons == ["Function is disabled"]


def test_nested_function_capability_resolves_direct_caller(tmp_path: Path, db_session: Session) -> None:
    caller = make_function(db_session, tmp_path, "caller")
    token = "nested-token"
    run = SkillRun(
        skill_id=caller.id,
        version_id=caller.active_version_id,
        status="running",
        started_at=datetime.now(UTC),
        invocation_source="skill",
        function_capability_token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
    )
    db_session.add(run)
    db_session.commit()

    resolved = InvocationContextFactory(
        db_session,
        project_root=tmp_path,
    ).from_runtime_capability(token)

    assert resolved.caller_skill_id == caller.id
    assert resolved.caller_version_id == caller.active_version_id
    assert resolved.caller_run_id == run.id


def test_function_capability_chain_can_continue_beyond_three_hops(
    tmp_path: Path,
    db_session: Session,
) -> None:
    functions = list(
        reversed(
            [
                make_function(
                    db_session,
                    tmp_path,
                    f"chain_{index}",
                    requirements=[f"chain_{index + 1}"] if index < 5 else [],
                )
                for index in range(5, 0, -1)
            ]
        )
    )
    token = "chain-root-token"
    active_run = SkillRun(
        skill_id=functions[0].id,
        version_id=functions[0].active_version_id,
        status="running",
        started_at=datetime.now(UTC),
        invocation_source="direct_user",
        function_capability_token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
    )
    db_session.add(active_run)
    db_session.commit()
    runner = FakeRunner(db_session)
    registry = service(db_session, tmp_path, runner)

    parent_run_id = active_run.id
    for target in functions[1:]:
        context = InvocationContextFactory(
            db_session,
            project_root=tmp_path,
        ).from_runtime_capability(token)
        run = invoke_registry(registry, target, {"value": target.name}, context)
        assert run.status == "succeeded"
        context = runner.calls[-1]["context"]
        assert context.parent_run_id == parent_run_id
        assert context.capability_token is not None
        token = context.capability_token
        parent_run_id = run.id
        run.status = "running"
        run.ended_at = None
        db_session.commit()

    assert len(runner.calls) == 4
    assert [call["context"].caller_skill_id for call in runner.calls] == [
        function.id for function in functions[:-1]
    ]


def test_function_cycle_is_rejected_by_graph_availability(
    tmp_path: Path,
    db_session: Session,
) -> None:
    target = make_function(db_session, tmp_path, "cycle_a")
    caller = make_function(db_session, tmp_path, "cycle_b")
    for skill, requirement in ((target, caller.name), (caller, target.name)):
        manifest_path = tmp_path / skill.manifest_path
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["function_requirements"] = [requirement]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        skill.function_requirements_json = [requirement]
    token = "cycle-token"
    active_run = SkillRun(
        skill_id=caller.id,
        version_id=caller.active_version_id,
        status="running",
        started_at=datetime.now(UTC),
        invocation_source="skill",
        function_capability_token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
    )
    db_session.add(active_run)
    db_session.commit()
    runner = FakeRunner(db_session)

    with pytest.raises(FunctionRegistryError, match="cycle"):
        InvocationContextFactory(
            db_session,
            project_root=tmp_path,
        ).from_runtime_capability(token)

    assert runner.calls == []


def test_schedule_attributed_service_capability_can_invoke_declared_function(
    tmp_path: Path,
    db_session: Session,
) -> None:
    target = make_function(db_session, tmp_path, "normalize_text")
    caller = make_function(db_session, tmp_path, "daily_service", requirements=[target.name])
    manifest_path = tmp_path / caller.manifest_path
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime"] = "service"
    manifest["schedule"] = {
        "type": "daily",
        "time": "08:00",
        "timezone": "America/Toronto",
        "input": {"value": "Hello"},
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    caller.runtime = "service"
    active_version = db_session.get(SkillVersion, caller.active_version_id)
    assert active_version is not None
    active_version.manifest_json = manifest
    permission_service = PermissionService(db_session, project_root=tmp_path)
    permission_service.approve_request(permission_service.create_runtime_request(caller))
    schedule = SkillSchedule(
        skill_id=caller.id,
        name="Daily service",
        status="active",
        schedule_type="daily",
        schedule_json=manifest["schedule"],
        input_json={"value": "Hello"},
        timezone="America/Toronto",
    )
    db_session.add(schedule)
    db_session.flush()
    token = "service-token"
    caller_run = SkillRun(
        skill_id=caller.id,
        version_id=caller.active_version_id,
        status="running",
        started_at=datetime.now(UTC),
        invocation_source="schedule",
        source_schedule_id=schedule.id,
        function_capability_token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
    )
    db_session.add(caller_run)
    db_session.commit()
    runner = FakeRunner(db_session)

    registry = service(db_session, tmp_path, runner)
    context = InvocationContextFactory(
        db_session,
        project_root=tmp_path,
    ).from_runtime_capability(token)
    run = invoke_registry(
        registry,
        target,
        {"value": "Hello"},
        context,
    )

    assert run.status == "succeeded"
    assert run.caller_skill_id == caller.id
    assert runner.calls[0]["context"].invocation_source == "skill"
    assert runner.calls[0]["context"].capability_token is not None
