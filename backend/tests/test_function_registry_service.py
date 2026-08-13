import hashlib
import json
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import ApprovalRequest, Skill, SkillRun, SkillVersion
from app.services.function_catalog_service import FunctionCatalogService
from app.services.function_registry_service import (
    FunctionCaller,
    FunctionRegistryError,
    FunctionRegistryService,
)
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
            initiating_action=context.initiating_action if context else None,
            function_capability_token_hash=context.capability_token_hash if context else None,
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run


def make_function(
    db: Session,
    project_root: Path,
    name: str,
    *,
    requirements: list[str] | None = None,
    network: list[str] | None = None,
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
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


def test_unified_catalog_persists_categories_states_and_user_lifecycle(
    tmp_path: Path,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection_state = {"connected": False}
    monkeypatch.setattr(
        "app.services.function_catalog_service.build_default_integration_service",
        lambda _db: SimpleNamespace(
            connection_status=lambda: SimpleNamespace(connected=connection_state["connected"]),
            provider_connected=lambda provider: connection_state["connected"] if provider == "github" else False,
        ),
    )
    target = make_function(db_session, tmp_path, "normalize_text")
    catalog = FunctionCatalogService(db_session, project_root=tmp_path)

    catalog.register_user_function(target)
    entries = {entry["id"]: entry for entry in catalog.list_entries(refresh=False)}

    assert (tmp_path / "runtime" / "function_catalog.json").is_file()
    assert entries["backend.codex.call"]["category"] == "backend_core"
    assert entries["backend.codex.call"]["availability"] == "available"
    assert entries["normalize_text"]["category"] == "user"
    assert entries["normalize_text"]["availability"] == "available"
    assert entries["github.repository.get"]["category"] == "integration"
    assert entries["github.repository.get"]["availability"] == "unavailable"
    assert entries["github.repository.get"]["availability_reasons"] == [
        "GitHub connection is not configured"
    ]
    available_index = {entry["id"]: entry for entry in catalog.available_index()}
    assert available_index["backend.codex.call"]["risk_level"] == "low"
    assert "input_schema" not in available_index["backend.codex.call"]
    assert "github.repository.get" not in available_index

    connection_state["connected"] = True
    connected = {entry["id"]: entry for entry in catalog.list_entries()}
    assert connected["github.repository.get"]["availability"] == "available"
    assert "github.repository.get" in {
        entry["id"] for entry in catalog.available_index()
    }

    target.enabled = False
    db_session.commit()
    disabled = {entry["id"]: entry for entry in catalog.list_entries()}
    assert disabled["normalize_text"]["availability"] == "disabled"

    ProposedSkillService(db_session, project_root=tmp_path).delete_skill(target)
    remaining_ids = {entry["id"] for entry in catalog.list_entries(refresh=False)}
    assert "normalize_text" not in remaining_ids


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

    run = registry.invoke_declared(
        FunctionCaller(caller, caller.active_version_id),
        target.name,
        {"value": "Hello"},
        source="skill",
        initiating_action="test_call",
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


def test_undeclared_function_call_is_blocked_before_execution(tmp_path: Path, db_session: Session) -> None:
    target = make_function(db_session, tmp_path, "normalize_text")
    caller = make_function(db_session, tmp_path, "caller")
    runner = FakeRunner(db_session)

    run = service(db_session, tmp_path, runner).invoke_declared(
        FunctionCaller(caller, caller.active_version_id),
        target.name,
        {"value": "Hello"},
        source="skill",
        initiating_action="test_call",
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
    blocked = registry.invoke_declared(
        FunctionCaller(caller, caller.active_version_id),
        target.name,
        {"value": "Hello"},
        source="skill",
        initiating_action="test_call",
    )
    assert blocked.status == "blocked"
    request = db_session.get(ApprovalRequest, review[0].approval_request_id)
    PermissionService(db_session, project_root=tmp_path).approve_request(request)

    allowed = registry.invoke_declared(
        FunctionCaller(caller, caller.active_version_id),
        target.name,
        {"value": "Hello"},
        source="skill",
        initiating_action="test_call",
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
    run = registry.invoke_declared(
        FunctionCaller(caller, caller.active_version_id),
        target.name,
        {"value": "Hello"},
        source="skill",
        initiating_action="test_call",
    )
    assert run.status == "blocked"
    assert "Caller-specific approval is required" in run.error_message


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

    bad_input = registry.invoke_declared(
        FunctionCaller(caller, caller.active_version_id),
        target.name,
        {"value": 123},
        source="skill",
        initiating_action="test_call",
    )
    assert bad_input.status == "blocked"
    assert "input JSON is incompatible" in bad_input.error_message
    assert runner.calls == []

    bad_output = registry.invoke_declared(
        FunctionCaller(caller, caller.active_version_id),
        target.name,
        {"value": "Hello"},
        source="skill",
        initiating_action="test_call",
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


def test_nested_function_capability_is_rejected(tmp_path: Path, db_session: Session) -> None:
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

    with pytest.raises(FunctionRegistryError, match="Nested function calls"):
        FunctionRegistryService(db_session, project_root=tmp_path).caller_from_capability(token)
