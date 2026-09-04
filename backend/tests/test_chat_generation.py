import json
import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi import HTTPException
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import AgentRun, ApprovalRequest, MemoryFact, Message, Skill, SkillGenerationRequest
from app.routers import chat as chat_router
from app.schemas.codex_routing import ResolvedInvocationSettings
from app.schemas.skill_generation import ChatRequest, ChatResponse
from app.services.agent_workflow_service import AgentWorkflowError
from app.services.chat_orchestrator import ChatOrchestrator
from app.services.codex_output_schema import output_schema_for_action
from app.services.codex_service import (
    CODEX_ACTION_TIMEOUT_SECONDS,
    DEFAULT_CODEX_ACTION_TIMEOUT_SECONDS,
    CodexGenerationError,
    CodexService,
    RealCodexAdapter,
    UnavailableCodexAdapter,
    codex_action_timeout_seconds,
    codex_process_registry,
    default_codex_adapter,
)
from app.services.manifest_validator import validate_manifest_file
from app.services.permission_service import PermissionService
from app.services.product_manager_contract_service import ProductManagerContractError
from app.services.product_manager_session_service import ProductManagerTurnResult
from app.services.proposed_skill_service import ProposedSkillService
from tests.fakes.codex import DeterministicCodexStub


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


class RecordingCodexAdapter:
    def __init__(
        self,
        network: list[str] | None = None,
        shell: bool = False,
        include_instructions: bool = False,
    ) -> None:
        self.called = False
        self.network = network
        self.shell = shell
        self.include_instructions = include_instructions

    def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
        self.called = True
        permissions = dict(plan["requested_permissions"])
        if self.network is not None:
            permissions["network"] = self.network
        if self.shell:
            permissions["shell"] = True
        manifest = {
            "name": plan["skill_name"],
            "description": plan["goal"],
            "entrypoint": "skill.py",
            "instructions_path": "SKILL.md" if self.include_instructions else None,
            "risk_level": "high" if permissions["shell"] else "medium" if permissions["network"] else "low",
            "permissions": permissions,
            "schedule": None,
            "created_by": "codex",
            "enabled": False,
        }
        (output_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (output_dir / "README.md").write_text("# Generated Skill\n", encoding="utf-8")
        if self.include_instructions:
            (output_dir / "SKILL.md").write_text("# Instructions\n", encoding="utf-8")
        (output_dir / "skill.py").write_text(
            "from pathlib import Path\n"
            "Path('task_executed.txt').write_text('executed', encoding='utf-8')\n",
            encoding="utf-8",
        )
        tests_dir = output_dir / "tests"
        assert tests_dir.is_dir()
        (tests_dir / "test_skill.py").write_text("def test_generated():\n    assert True\n", encoding="utf-8")
        return subprocess.CompletedProcess(args=["recording-codex"], returncode=0, stdout="ok", stderr="")


class FixedBlueprintAdapter(DeterministicCodexStub):
    def __init__(self, plan: dict | None = None) -> None:
        self.plan = plan or skill_plan()
        self.called = False

    def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
        if plan.get("codex_task") != "product_manager_plan_build":
            return super().generate(prompt, output_dir, plan)
        self.called = True
        source = self.plan
        permission_plan = {
            "build_time": {
                "internet_research": bool(
                    source.get("requested_network_domains") or source.get("requested_dependencies")
                ),
                "dependencies": list(source.get("requested_dependencies", [])),
            },
            "runtime": {
                "dependencies": list(source.get("requested_dependencies", [])),
                "network": list(source.get("requested_network_domains", [])),
                "codex": {
                    "call_response": bool(
                        (source.get("requested_permissions") or {}).get("codex", {}).get("call_response", False)
                    ),
                    "internet_access": False,
                },
            },
        }
        blueprint = {
            "name": source["skill_name"],
            "description": source["goal"],
            "runtime": source.get("runtime", "function"),
            "input_schema": source.get("input_schema"),
            "output_schema": source.get("output_schema"),
            "expected_behavior": ["Implement the requested capability."],
            "functions": [],
            "schedule": source.get("schedule"),
        }
        return subprocess.CompletedProcess(
            args=["fixed-blueprint"],
            returncode=0,
            stdout=json.dumps(
                {
                    "decision": "proceed_to_approval",
                    "user_prompt": None,
                    "build_workflow": "task_dag",
                    "blueprint": blueprint,
                    "permission_plan": permission_plan,
                }
            ),
            stderr="",
        )


def approve_build_time_permissions(db_session: Session, generation_request: SkillGenerationRequest) -> None:
    if "skill_name" not in generation_request.plan_json:
        prepared_plan = skill_plan()
        generation_request.plan_json = prepared_plan
        generation_request.proposed_skill_name = prepared_plan["skill_name"]
        generation_request.proposed_display_name = prepared_plan["display_name"]
        generation_request.requested_permissions_json = prepared_plan["requested_permissions"]
        generation_request.requested_dependencies_json = prepared_plan["requested_dependencies"]
        generation_request.requested_network_domains_json = prepared_plan["requested_network_domains"]
        generation_request.risk_level = prepared_plan["risk_level"]
        db_session.commit()
    permission_service = PermissionService(db_session)
    request = permission_service.create_build_time_request(generation_request)
    permission_service.approve_request(request)
    generation_request.status = "approved"
    db_session.commit()


def skill_plan(**overrides) -> dict:
    plan = {
        "goal": "Create a reusable local workflow skill.",
        "skill_name": "generated_skill",
        "display_name": "Generated Skill",
        "files_to_generate": ["manifest.json", "README.md", "skill.py", "tests/test_skill.py"],
        "expected_input": {"input": "object"},
        "expected_output": {"title": "string", "items": [], "warnings": []},
        "input_schema": {"type": "object", "additionalProperties": True},
        "output_schema": {"type": "object", "additionalProperties": True},
        "requested_permissions": {
            "network": [],
            "filesystem_read": [],
            "filesystem_write": ["./cache"],
            "secrets": [],
            "shell": False,
            "codex": {"call_response": False, "internet_access": False},
        },
        "requested_network_domains": [],
        "requested_dependencies": [],
        "tests_required": True,
        "validation_steps": [
            "validate manifest.json",
            "inspect generated files",
            "run skill tests",
        ],
        "risk_level": "low",
        "automatic_actions_blocked": [
            "installing the skill",
            "running the skill",
            "installing packages without approval",
        ],
    }
    plan.update(overrides)
    return plan


def test_project_mode_creates_skill_proposal(db_session: Session) -> None:
    codex_adapter = FixedBlueprintAdapter(
        skill_plan(
            skill_name="ai_infra_news_digest",
            display_name="Ai Infra News Digest",
            requested_permissions={
                "network": ["nvidia.com", "amd.com"],
                "filesystem_read": [],
                "filesystem_write": ["./cache"],
                "secrets": [],
                "shell": False,
            },
            requested_network_domains=["nvidia.com", "amd.com"],
            requested_dependencies=["requests"],
            risk_level="medium",
        )
    )
    response = ChatOrchestrator(
        db_session,
        codex_service=CodexService(db_session, adapter=codex_adapter),
    ).handle_message(
        "Create a reusable skill that summarizes AI chip news from Nvidia and AMD.",
    )

    assert response["type"] == "skill_generation_plan"
    generation_request = response["generation_request"]
    permission_request = response["permission_request"]
    assert generation_request.status == "awaiting_approval"
    assert generation_request.plan_json["skill_name"] == "ai_infra_news_digest"
    assert generation_request.plan_json["runtime"] == "function"
    assert codex_adapter.called is True
    assert permission_request.request_scope == "build_time"
    assert permission_request.status == "pending"


def test_removed_chat_mode_is_rejected_by_the_request_contract() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ChatRequest.model_validate({"message": "What is inflation?", "mode": "chat"})


def test_chat_response_model_serializes_generation_request_fields(db_session: Session) -> None:
    codex_adapter = FixedBlueprintAdapter(
        skill_plan(skill_name="ai_infra_news_digest", display_name="Ai Infra News Digest")
    )
    response = ChatOrchestrator(
        db_session,
        codex_service=CodexService(db_session, adapter=codex_adapter),
    ).handle_message(
        "Create a reusable skill that summarizes AI chip news from Nvidia and AMD.",
    )
    data = TypeAdapter(ChatResponse).validate_python(response).model_dump(mode="json")

    assert data["type"] == "skill_generation_plan"
    assert isinstance(data["generation_request"]["id"], int)
    assert data["generation_request"]["proposed_display_name"] == "Ai Infra News Digest"
    assert isinstance(data["permission_request"]["id"], int)


def test_project_mode_uses_one_product_manager_planning_action(db_session: Session) -> None:
    codex_adapter = DeterministicCodexStub()
    tasks: list[str] = []
    prompts: dict[str, str] = {}

    class RecordingAdapter(DeterministicCodexStub):
        def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
            task = str(plan.get("codex_task"))
            tasks.append(task)
            prompts[task] = prompt
            return codex_adapter.generate(prompt, output_dir, plan)

    response = ChatOrchestrator(
        db_session,
        codex_service=CodexService(db_session, adapter=RecordingAdapter()),
    ).handle_message(
        "Create a reusable skill that summarizes AI chip news from Nvidia and AMD.",
    )

    generation_request = response["generation_request"]
    assert tasks[0] == "product_manager_plan_build"
    assert "product_manager_refine_intent" not in tasks
    assert "deciding whether and how" in prompts["product_manager_plan_build"]
    assert "matching the supplied output schema" in prompts["product_manager_plan_build"]
    assert "Expected JSON syntax" not in prompts["product_manager_plan_build"]
    assert '"expected_files"' not in prompts["product_manager_plan_build"]
    assert "For `write_task_dag`, return" not in prompts["product_manager_plan_build"]
    agent_run = db_session.query(AgentRun).filter_by(generation_request_id=generation_request.id).one()
    steps = sorted(agent_run.steps, key=lambda step: step.id)
    decisions = [
        step.output_json["decision_json"]["decision"]
        for step in steps
        if (step.output_json or {}).get("decision_json")
    ]
    assert decisions == ["proceed_to_approval"]
    permission_step = next(step for step in steps if step.action == "backend_build_time_permission_review")
    assert permission_step.step_name == "backend"
    assert permission_step.approval_request_id == response["permission_request"].id
    assert permission_step.input_json is None
    assert permission_step.output_json is None
    assert "plausibility_review" not in generation_request.plan_json


def test_project_build_instructions_are_colocated_with_workflow_packages() -> None:
    app_dir = Path(__file__).resolve().parents[1] / "app"
    workflow_root = app_dir / "workflows"

    assert not (workflow_root / "common" / "instructions" / "refine_intent.md").exists()
    assert (workflow_root / "common" / "instructions" / "plan_build.md").is_file()
    assert (workflow_root / "task_dag" / "instructions" / "product_manager.md").is_file()
    assert (workflow_root / "task_dag" / "instructions" / "builder.md").is_file()
    assert (workflow_root / "task_dag" / "instructions" / "repair.md").is_file()
    assert (workflow_root / "task_dag" / "instructions" / "tester.md").is_file()
    assert (workflow_root / "single_codex" / "instructions" / "run.md").is_file()
    assert not (app_dir / "agent_instructions" / "product_manager" / "task_dag.md").exists()
    assert not (app_dir / "agent_instructions" / "workflows" / "single_codex.md").exists()


def test_unsupported_project_reports_pm_reason_without_artifacts(db_session: Session, tmp_path: Path) -> None:
    class UnsupportedAdapter(DeterministicCodexStub):
        def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
            if plan.get("codex_task") == "product_manager_plan_build":
                return subprocess.CompletedProcess(
                    args=["fake"],
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "decision": "stop_inplausible",
                            "user_prompt": "File deletion is blocked. Ask for safe manual cleanup guidance instead.",
                            "build_workflow": None,
                            "blueprint": None,
                            "permission_plan": None,
                        }
                    ),
                    stderr="",
                )
            return super().generate(prompt, output_dir, plan)

    response = ChatOrchestrator(
        db_session,
        codex_service=CodexService(db_session, adapter=UnsupportedAdapter(), project_root=tmp_path),
    ).handle_message("Make a skill that deletes files automatically.")

    assert response == {
        "type": "project_not_plausible",
        "message": "I would not turn that into a skill yet.",
        "reason": "File deletion is blocked. Ask for safe manual cleanup guidance instead.",
    }
    generation_request = db_session.query(SkillGenerationRequest).one()
    assert generation_request.status == "failed"
    assert not (tmp_path / "runtime" / "agent_runs" / "run_1" / "blueprint.json").exists()


def test_unclear_project_asks_for_input_then_same_chat_reply_builds_plan(
    db_session: Session,
    tmp_path: Path,
) -> None:
    class ClarifyingAdapter(DeterministicCodexStub):
        def __init__(self) -> None:
            self.plan_calls = 0

        def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
            if plan.get("codex_task") == "product_manager_plan_build":
                self.plan_calls += 1
                if self.plan_calls <= 2:
                    return subprocess.CompletedProcess(
                        args=["fake"],
                        returncode=0,
                        stdout=json.dumps(
                            {
                                "decision": "ask_user_for_input",
                                "user_prompt": "What repeated task should this skill help with?",
                                "build_workflow": None,
                                "blueprint": None,
                                "permission_plan": None,
                            }
                        ),
                        stderr="",
                    )
            return super().generate(prompt, output_dir, plan)

    codex_adapter = ClarifyingAdapter()
    orchestrator = ChatOrchestrator(
        db_session,
        codex_service=CodexService(db_session, adapter=codex_adapter, project_root=tmp_path),
    )
    first_response = orchestrator.handle_message("Build me something useful.", conversation_id="chat-1")

    assert first_response["type"] == "project_needs_input"
    generation_request = first_response["generation_request"]
    assert generation_request.status == "needs_input"
    assert not (tmp_path / "runtime" / "agent_runs" / f"run_{first_response['agent_run'].id}" / "blueprint.json").exists()

    second_response = orchestrator.handle_message(
        "Make it summarize recurring local meeting notes into action items.",
        generation_request_id=generation_request.id,
        conversation_id="chat-1",
    )

    assert second_response["type"] == "project_needs_input"
    third_response = orchestrator.handle_message(
        "The notes are Markdown files selected by the user each time.",
        generation_request_id=generation_request.id,
        conversation_id="chat-1",
    )

    assert third_response["type"] == "skill_generation_plan"
    db_session.refresh(generation_request)
    assert generation_request.status == "awaiting_approval"
    assert codex_adapter.plan_calls == 3
    assert "Markdown files selected by the user" in generation_request.user_message
    agent_run = db_session.query(AgentRun).filter_by(generation_request_id=generation_request.id).one()
    assert (tmp_path / "runtime" / "agent_runs" / f"run_{agent_run.id}" / "blueprint.json").is_file()


def test_project_clarification_fallback_matches_the_exact_conversation(db_session: Session) -> None:
    orchestrator = ChatOrchestrator(db_session)
    first = orchestrator.create_generation_request("First", conversation_id="chat-1")
    second = orchestrator.create_generation_request("Second", conversation_id="chat-2")
    first.status = "needs_input"
    second.status = "needs_input"
    db_session.commit()

    resumed = orchestrator._project_generation_request_for_message(
        "Reply to the older matching conversation",
        generation_request_id=None,
        conversation_id="chat-1",
    )

    assert resumed.id == first.id
    assert "Reply to the older matching conversation" in resumed.user_message
    assert "Reply to the older matching conversation" not in second.user_message


def test_project_clarification_rejects_cross_conversation_request_id(db_session: Session) -> None:
    orchestrator = ChatOrchestrator(db_session)
    request = orchestrator.create_generation_request("First", conversation_id="chat-1")
    request.status = "needs_input"
    db_session.commit()

    with pytest.raises(AgentWorkflowError, match="different conversation"):
        orchestrator._project_generation_request_for_message(
            "Wrong chat",
            generation_request_id=request.id,
            conversation_id="chat-2",
        )


def test_project_mode_does_not_use_backend_unsafe_keyword_heuristic(db_session: Session) -> None:
    response = ChatOrchestrator(db_session).handle_message(
        "Make a skill that deletes files automatically.",
    )

    assert response["type"] == "skill_generation_plan"
    assert db_session.query(SkillGenerationRequest).count() == 1


def test_product_manager_can_reject_after_clarification_without_creating_artifacts(
    db_session: Session,
    tmp_path: Path,
) -> None:
    class RejectAfterClarificationAdapter(DeterministicCodexStub):
        def __init__(self) -> None:
            self.plan_calls = 0

        def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
            if plan.get("codex_task") == "product_manager_plan_build":
                self.plan_calls += 1
                decision = (
                    {
                        "decision": "ask_user_for_input",
                        "user_prompt": "Should the skill delete the source files after processing?",
                        "build_workflow": None,
                        "blueprint": None,
                        "permission_plan": None,
                    }
                    if self.plan_calls == 1
                    else {
                        "decision": "stop_inplausible",
                        "user_prompt": (
                            "Automatic file deletion is blocked. "
                            "Generate a non-destructive cleanup report instead."
                        ),
                        "build_workflow": None,
                        "blueprint": None,
                        "permission_plan": None,
                    }
                )
                return subprocess.CompletedProcess(
                    args=["fake"], returncode=0, stdout=json.dumps(decision), stderr=""
                )
            return super().generate(prompt, output_dir, plan)

    adapter = RejectAfterClarificationAdapter()
    orchestrator = ChatOrchestrator(
        db_session,
        codex_service=CodexService(db_session, adapter=adapter, project_root=tmp_path),
    )
    first = orchestrator.handle_message("Build a cleanup skill.")
    second = orchestrator.handle_message(
        "Yes, delete them automatically.",
        generation_request_id=first["generation_request"].id,
    )

    assert second["type"] == "project_not_plausible"
    generation_request = db_session.get(SkillGenerationRequest, first["generation_request"].id)
    agent_run = db_session.query(AgentRun).filter_by(generation_request_id=generation_request.id).one()
    artifact_root = tmp_path / "runtime" / "agent_runs" / f"run_{agent_run.id}"
    assert generation_request.status == "failed"
    assert generation_request.proposed_skill_id is None
    assert not (artifact_root / "blueprint.json").exists()
    assert not (artifact_root / "permissions.json").exists()
    assert db_session.query(Skill).count() == 0


def test_product_manager_unavailable_function_fails_without_build_artifacts(
    db_session: Session,
    tmp_path: Path,
) -> None:
    class UnknownFunctionAdapter(FixedBlueprintAdapter):
        def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
            result = super().generate(prompt, output_dir, plan)
            if plan.get("codex_task") != "product_manager_plan_build":
                return result
            output = json.loads(result.stdout)
            output["blueprint"]["functions"] = ["missing.function"]
            return subprocess.CompletedProcess(
                args=result.args,
                returncode=0,
                stdout=json.dumps(output),
                stderr="",
            )

    orchestrator = ChatOrchestrator(
        db_session,
        codex_service=CodexService(
            db_session,
            adapter=UnknownFunctionAdapter(),
            project_root=tmp_path,
        ),
    )
    with pytest.raises(AgentWorkflowError, match="Unknown function id: missing.function"):
        orchestrator.handle_message("Build a reusable local workflow skill.")

    generation_request = db_session.query(SkillGenerationRequest).one()
    agent_run = db_session.query(AgentRun).filter_by(generation_request_id=generation_request.id).one()
    artifact_root = tmp_path / "runtime" / "agent_runs" / f"run_{agent_run.id}"
    assert generation_request.status == "failed"
    assert generation_request.error_message == "Unknown function id: missing.function"
    assert generation_request.proposed_skill_id is None
    assert db_session.query(ApprovalRequest).count() == 0
    assert not (artifact_root / "blueprint.json").exists()
    assert not (artifact_root / "permissions.json").exists()


def test_product_manager_blocked_permission_field_fails_closed(db_session: Session) -> None:
    result = FixedBlueprintAdapter().generate(
        "plan",
        Path.cwd(),
        {"codex_task": "product_manager_plan_build"},
    )
    response = json.loads(result.stdout)
    response["permission_plan"]["runtime"]["filesystem_write"] = ["C:/"]

    with pytest.raises(ProductManagerContractError, match="invalid planning response"):
        CodexService(db_session).product_manager_contracts.sanitize_plan_build(response, {})


def test_product_manager_incomplete_function_schema_fails_closed(db_session: Session) -> None:
    result = FixedBlueprintAdapter().generate(
        "plan",
        Path.cwd(),
        {"codex_task": "product_manager_plan_build"},
    )
    response = json.loads(result.stdout)
    response["blueprint"]["input_schema"] = None

    with pytest.raises(ProductManagerContractError, match="complete object-shaped input_schema"):
        CodexService(db_session).product_manager_contracts.sanitize_plan_build(response, {})


def test_generation_request_contains_plan_permissions_and_dependencies(db_session: Session) -> None:
    codex_adapter = FixedBlueprintAdapter(
        skill_plan(
            requested_permissions={
                "network": ["nvidia.com", "amd.com"],
                "filesystem_read": [],
                "filesystem_write": ["./cache"],
                "secrets": [],
                "shell": False,
            },
            requested_network_domains=["nvidia.com", "amd.com"],
            requested_dependencies=["requests"],
            risk_level="medium",
        )
    )
    response = ChatOrchestrator(
        db_session,
        codex_service=CodexService(db_session, adapter=codex_adapter),
    ).handle_message("Create an automation for tracking Nvidia and AMD news.")
    generation_request = response["generation_request"]

    assert generation_request.plan_json["files_to_generate"]
    assert generation_request.requested_permissions_json["network"]
    assert "requests" in generation_request.requested_dependencies_json
    assert generation_request.risk_level == "medium"


def test_build_time_permission_allows_skill_own_cache_read(db_session: Session) -> None:
    codex_adapter = FixedBlueprintAdapter(
        skill_plan(
            requested_permissions={
                "network": [],
                "filesystem_read": ["./cache"],
                "filesystem_write": ["./cache"],
                "secrets": [],
                "shell": False,
            },
        )
    )
    response = ChatOrchestrator(
        db_session,
        codex_service=CodexService(db_session, adapter=codex_adapter),
    ).handle_message("Create a local game tool with cache-backed state.")

    permission_request = response["permission_request"]
    assert permission_request.risk_level == "low"
    assert permission_request.requested_filesystem_json["filesystem_read"] == []
    assert permission_request.requested_filesystem_json["filesystem_write"] == []


def test_stale_blocked_build_time_request_is_refreshed_before_approval(db_session: Session) -> None:
    codex_adapter = FixedBlueprintAdapter(
        skill_plan(
            requested_permissions={
                "network": [],
                "filesystem_read": ["./cache"],
                "filesystem_write": ["./cache"],
                "secrets": [],
                "shell": False,
            },
        )
    )
    response = ChatOrchestrator(
        db_session,
        codex_service=CodexService(db_session, adapter=codex_adapter),
    ).handle_message("Create a local game tool with cache-backed state.")
    permission_request = response["permission_request"]
    permission_request.risk_level = "blocked"
    db_session.commit()

    approved = PermissionService(db_session).approve_request(permission_request)

    assert approved.status == "approved"
    assert approved.risk_level == "low"


def test_product_manager_blueprint_owns_runtime_and_io_schemas(db_session: Session) -> None:
    adapter = FixedBlueprintAdapter(
        skill_plan(
            skill_name="calculator_tool",
            display_name="Calculator Tool",
            input_schema={
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
            },
            output_schema={
                "type": "object",
                "properties": {"result": {"type": "number"}},
                "required": ["result"],
            },
        )
    )

    response = ChatOrchestrator(
        db_session,
        codex_service=CodexService(db_session, adapter=adapter),
    ).handle_message("Build a calculator tool.")
    plan = response["generation_request"].plan_json

    assert adapter.called is True
    assert plan["runtime"] == "function"
    assert plan["skill_name"] == "calculator_tool"
    assert plan["input_schema"]["properties"]["expression"]["type"] == "string"
    assert plan["output_schema"]["properties"]["result"]["type"] == "number"


def test_denying_generation_request_does_not_create_files(tmp_path: Path, db_session: Session) -> None:
    generation_request = ChatOrchestrator(db_session).create_generation_request(
        "Create a reusable local workflow skill."
    )
    generation_request.status = "cancelled"
    db_session.commit()

    proposed_dir = tmp_path / "skills" / "proposed" / generation_request.proposed_skill_name

    assert not proposed_dir.exists()
    assert generation_request.proposed_skill_id is None


def test_generation_is_not_invoked_before_build_time_permission_approval(
    tmp_path: Path,
    db_session: Session,
) -> None:
    generation_request = ChatOrchestrator(db_session).create_generation_request(
        "Create a reusable local workflow skill."
    )
    generation_request.status = "approved"
    db_session.commit()

    with pytest.raises(Exception, match="Build-time permissions have not been reviewed"):
        CodexService(
            db_session,
            adapter=RecordingCodexAdapter(),
            project_root=tmp_path,
        ).generate_from_request(generation_request)


def test_approving_generation_invokes_mock_codex_and_creates_proposed_skill(
    tmp_path: Path,
    db_session: Session,
) -> None:
    generation_request = ChatOrchestrator(db_session).create_generation_request(
        "Create a reusable local workflow skill."
    )
    approve_build_time_permissions(db_session, generation_request)
    adapter = RecordingCodexAdapter()

    skill, validation = CodexService(db_session, adapter=adapter, project_root=tmp_path).generate_from_request(
        generation_request
    )

    assert adapter.called is True
    assert skill.status == "proposed"
    assert skill.installed_path is None
    assert skill.enabled is False
    assert validation.ok is True
    assert generation_request.proposed_skill_id == skill.id
    runtime_request = PermissionService(db_session, project_root=tmp_path).create_runtime_request(skill)
    assert runtime_request.request_scope == "runtime"
    assert runtime_request.status == "pending"


def test_runtime_permission_request_uses_actual_manifest_permissions(
    tmp_path: Path,
    db_session: Session,
) -> None:
    generation_request = ChatOrchestrator(db_session).create_generation_request(
        "Create a reusable local workflow skill."
    )
    approve_build_time_permissions(db_session, generation_request)

    skill, _validation = CodexService(
        db_session,
        adapter=RecordingCodexAdapter(network=["example.com"]),
        project_root=tmp_path,
    ).generate_from_request(generation_request)

    runtime_request = PermissionService(db_session, project_root=tmp_path).create_runtime_request(skill)
    assert runtime_request.requested_permissions_json["network"] == ["example.com"]
    assert runtime_request.requested_network_domains_json == ["example.com"]


def test_permission_expansion_from_plan_to_manifest_is_detected(
    tmp_path: Path,
    db_session: Session,
) -> None:
    generation_request = ChatOrchestrator(db_session).create_generation_request(
        "Create a reusable local workflow skill."
    )
    approve_build_time_permissions(db_session, generation_request)

    skill, _validation = CodexService(
        db_session,
        adapter=RecordingCodexAdapter(network=["example.com"]),
        project_root=tmp_path,
    ).generate_from_request(generation_request)

    runtime_request = PermissionService(db_session, project_root=tmp_path).create_runtime_request(skill)
    assert runtime_request.reason_json["permission_expansion"] == {"network": ["example.com"]}


def test_codex_call_response_expansion_requires_runtime_approval(
    tmp_path: Path,
    db_session: Session,
) -> None:
    generation_request = ChatOrchestrator(db_session).create_generation_request(
        "Create a reusable local workflow skill."
    )
    approve_build_time_permissions(db_session, generation_request)
    skill, _validation = CodexService(
        db_session,
        adapter=RecordingCodexAdapter(),
        project_root=tmp_path,
    ).generate_from_request(generation_request)
    manifest_path = tmp_path / skill.manifest_path
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["permissions"] = {"codex": {"call_response": True}}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    runtime_request = PermissionService(db_session, project_root=tmp_path).create_runtime_request(skill)

    assert runtime_request.requested_permissions_json == {"codex": {"call_response": True}}
    assert runtime_request.reason_json["permission_expansion"] == {
        "codex": {"call_response": True}
    }


def test_empty_runtime_permissions_do_not_create_permission_expansion(
    tmp_path: Path,
    db_session: Session,
) -> None:
    generation_request = ChatOrchestrator(db_session).create_generation_request(
        "Create a reusable local workflow skill."
    )
    approve_build_time_permissions(db_session, generation_request)

    skill, _validation = CodexService(
        db_session,
        adapter=RecordingCodexAdapter(),
        project_root=tmp_path,
    ).generate_from_request(generation_request)

    runtime_request = PermissionService(db_session, project_root=tmp_path).create_runtime_request(skill)
    assert runtime_request.requested_permissions_json == {}
    assert runtime_request.risk_level == "low"
    assert runtime_request.reason_json["runner_unsupported"] == []
    assert runtime_request.reason_json["permission_expansion"] == {}
    assert "Permission expansion detected" not in runtime_request.user_explanation


def test_install_decision_requires_runtime_permission_approval(
    tmp_path: Path,
    db_session: Session,
) -> None:
    generation_request = ChatOrchestrator(db_session).create_generation_request(
        "Create a reusable local workflow skill."
    )
    approve_build_time_permissions(db_session, generation_request)
    skill, _validation = CodexService(
        db_session,
        adapter=RecordingCodexAdapter(),
        project_root=tmp_path,
    ).generate_from_request(generation_request)

    permission_service = PermissionService(db_session, project_root=tmp_path)
    pending_decision = permission_service.can_install(skill)
    assert pending_decision.allowed is False
    assert pending_decision.reason == "Runtime permission request is pending"

    request = permission_service.create_runtime_request(skill)
    permission_service.approve_request(request)
    approved_decision = permission_service.can_install(skill)
    assert approved_decision.allowed is True


def test_install_decision_blocks_denied_runtime_permissions(
    tmp_path: Path,
    db_session: Session,
) -> None:
    generation_request = ChatOrchestrator(db_session).create_generation_request(
        "Create a reusable local workflow skill."
    )
    approve_build_time_permissions(db_session, generation_request)
    skill, _validation = CodexService(
        db_session,
        adapter=RecordingCodexAdapter(),
        project_root=tmp_path,
    ).generate_from_request(generation_request)

    permission_service = PermissionService(db_session, project_root=tmp_path)
    request = permission_service.create_runtime_request(skill)
    permission_service.deny_request(request)

    decision = permission_service.can_install(skill)
    assert decision.allowed is False
    assert decision.reason == "Runtime permission request is denied"

    replacement = permission_service.create_runtime_request(skill)
    assert replacement.id != request.id
    assert replacement.status == "pending"
    db_session.refresh(request)
    assert request.status == "denied"


def test_runtime_approval_is_superseded_when_manifest_permissions_change(
    tmp_path: Path,
    db_session: Session,
) -> None:
    generation_request = ChatOrchestrator(db_session).create_generation_request(
        "Create a reusable local workflow skill."
    )
    approve_build_time_permissions(db_session, generation_request)
    skill, _validation = CodexService(
        db_session,
        adapter=RecordingCodexAdapter(),
        project_root=tmp_path,
    ).generate_from_request(generation_request)
    permission_service = PermissionService(db_session, project_root=tmp_path)
    approved = permission_service.create_runtime_request(skill)
    permission_service.approve_request(approved)

    manifest_path = tmp_path / skill.manifest_path
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["permissions"]["network"] = ["example.com"]
    manifest["permissions"]["codex"] = {"call_response": True, "internet_access": True}
    manifest["risk_level"] = "medium"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    decision = permission_service.can_install(skill)
    assert decision.allowed is False
    assert decision.reason == "Runtime permissions do not match the current manifest"

    replacement = permission_service.create_runtime_request(skill)
    db_session.refresh(approved)
    assert approved.status == "superseded"
    assert replacement.id != approved.id
    assert replacement.status == "pending"
    assert replacement.requested_permissions_json["network"] == ["example.com"]


def test_runtime_approval_migrates_legacy_fingerprint_when_contract_is_unchanged(
    tmp_path: Path,
    db_session: Session,
) -> None:
    generation_request = ChatOrchestrator(db_session).create_generation_request(
        "Create a reusable local workflow skill."
    )
    approve_build_time_permissions(db_session, generation_request)
    skill, _validation = CodexService(
        db_session,
        adapter=RecordingCodexAdapter(),
        project_root=tmp_path,
    ).generate_from_request(generation_request)
    permission_service = PermissionService(db_session, project_root=tmp_path)
    approved = permission_service.create_runtime_request(skill)
    permission_service.approve_request(approved)
    manifest = validate_manifest_file(tmp_path / skill.manifest_path)
    reason_json = dict(approved.reason_json or {})
    reason_json["manifest_permission_fingerprint"] = (
        permission_service._legacy_runtime_manifest_fingerprint(manifest)
    )
    reason_json.pop("function_graph_fingerprint", None)
    approved.reason_json = reason_json
    db_session.commit()

    decision = permission_service.can_install(skill)

    assert decision.allowed is True
    db_session.refresh(approved)
    assert (approved.reason_json or {}).get("manifest_permission_fingerprint") == (
        permission_service._runtime_manifest_fingerprint(skill, manifest)
    )
    assert (approved.reason_json or {}).get("function_graph_fingerprint")


def test_runtime_permissions_cannot_be_reviewed_for_unfinalized_skill(
    tmp_path: Path,
    db_session: Session,
) -> None:
    skill = Skill(
        name="still_building",
        description="Still building.",
        runtime="function",
        status="building",
        risk_level="low",
        manifest_path="skills/proposed/still_building/manifest.json",
    )
    db_session.add(skill)
    db_session.commit()

    with pytest.raises(Exception, match="only after a skill has passed validation"):
        PermissionService(db_session, project_root=tmp_path).create_runtime_request(skill)


def test_blocked_permissions_cannot_be_approved(tmp_path: Path, db_session: Session) -> None:
    generation_request = ChatOrchestrator(db_session).create_generation_request(
        "Create a reusable local workflow skill."
    )
    approve_build_time_permissions(db_session, generation_request)
    skill, _validation = CodexService(
        db_session,
        adapter=RecordingCodexAdapter(shell=True),
        project_root=tmp_path,
    ).generate_from_request(generation_request)

    permission_service = PermissionService(db_session, project_root=tmp_path)
    request = permission_service.create_runtime_request(skill)

    assert request.risk_level == "blocked"
    with pytest.raises(Exception, match="Blocked or unsupported"):
        permission_service.approve_request(request)


def test_run_decision_blocks_without_runtime_approval(tmp_path: Path, db_session: Session) -> None:
    generation_request = ChatOrchestrator(db_session).create_generation_request(
        "Create a reusable local workflow skill."
    )
    approve_build_time_permissions(db_session, generation_request)
    skill, _validation = CodexService(
        db_session,
        adapter=RecordingCodexAdapter(),
        project_root=tmp_path,
    ).generate_from_request(generation_request)

    decision = PermissionService(db_session, project_root=tmp_path).can_run(skill)
    assert decision.allowed is False
    assert decision.reason == "Runtime permission request is pending"


def test_run_decision_allows_approved_network_permissions(
    tmp_path: Path,
    db_session: Session,
) -> None:
    generation_request = ChatOrchestrator(db_session).create_generation_request(
        "Create an automation for tracking Nvidia and AMD news."
    )
    approve_build_time_permissions(db_session, generation_request)
    skill, _validation = CodexService(
        db_session,
        adapter=RecordingCodexAdapter(network=["nvidia.com"]),
        project_root=tmp_path,
    ).generate_from_request(generation_request)
    permission_service = PermissionService(db_session, project_root=tmp_path)
    request = permission_service.create_runtime_request(skill)
    permission_service.approve_request(request)

    decision = permission_service.can_run(skill)
    assert decision.allowed is True


def test_run_decision_allows_approved_supported_permissions(
    tmp_path: Path,
    db_session: Session,
) -> None:
    generation_request = ChatOrchestrator(db_session).create_generation_request(
        "Create a reusable local workflow skill."
    )
    approve_build_time_permissions(db_session, generation_request)
    skill, _validation = CodexService(
        db_session,
        adapter=RecordingCodexAdapter(),
        project_root=tmp_path,
    ).generate_from_request(generation_request)
    permission_service = PermissionService(db_session, project_root=tmp_path)
    request = permission_service.create_runtime_request(skill)
    permission_service.approve_request(request)

    decision = permission_service.can_run(skill)
    assert decision.allowed is True


def test_approved_request_cannot_be_denied_inconsistently(tmp_path: Path, db_session: Session) -> None:
    generation_request = ChatOrchestrator(db_session).create_generation_request(
        "Create a reusable local workflow skill."
    )
    request = PermissionService(db_session).create_build_time_request(generation_request)
    permission_service = PermissionService(db_session)
    permission_service.approve_request(request)

    with pytest.raises(Exception, match="Approved permission requests cannot be denied"):
        permission_service.deny_request(request)


def test_generated_skill_is_not_installed_or_run_automatically(tmp_path: Path, db_session: Session) -> None:
    generation_request = ChatOrchestrator(db_session).create_generation_request(
        "Create a reusable local workflow skill."
    )
    approve_build_time_permissions(db_session, generation_request)

    skill, _validation = CodexService(
        db_session,
        adapter=RecordingCodexAdapter(),
        project_root=tmp_path,
    ).generate_from_request(generation_request)

    skill_dir = ProposedSkillService(db_session, project_root=tmp_path).skill_dir_for_record(skill)
    assert skill.status == "proposed"
    assert not (skill_dir / "task_executed.txt").exists()


def test_generated_skill_with_optional_instructions_runs_tests_but_not_skill_task(
    tmp_path: Path,
    db_session: Session,
) -> None:
    generation_request = ChatOrchestrator(db_session).create_generation_request(
        "Create a workflow skill with reusable instructions."
    )
    approve_build_time_permissions(db_session, generation_request)
    generation_request.plan_json["files_to_generate"].append("SKILL.md")
    db_session.commit()

    skill, validation = CodexService(
        db_session,
        adapter=RecordingCodexAdapter(include_instructions=True),
        project_root=tmp_path,
    ).generate_from_request(generation_request)

    skill_dir = ProposedSkillService(db_session, project_root=tmp_path).skill_dir_for_record(skill)
    assert skill.instructions_path == "SKILL.md"
    assert (skill_dir / "SKILL.md").is_file()
    assert validation.tests_run is True
    assert validation.tests_passed is True
    assert not (skill_dir / "task_executed.txt").exists()


def test_network_requesting_generated_skill_can_be_approved_for_runtime(
    tmp_path: Path,
    db_session: Session,
) -> None:
    generation_request = ChatOrchestrator(db_session).create_generation_request(
        "Create an automation for tracking Nvidia and AMD news."
    )
    approve_build_time_permissions(db_session, generation_request)
    skill, validation = CodexService(
        db_session,
        adapter=RecordingCodexAdapter(network=["nvidia.com"]),
        project_root=tmp_path,
    ).generate_from_request(generation_request)

    assert validation.warnings == [
        "This skill requests network access. Runtime execution requires explicit approval and uses container network access in the current MVP."
    ]

    permission_service = PermissionService(db_session, project_root=tmp_path)
    runtime_request = permission_service.create_runtime_request(skill)
    permission_service.approve_request(runtime_request)

    assert runtime_request.requested_network_domains_json == ["nvidia.com"]
    assert permission_service.can_run(skill).allowed is True


def test_real_codex_adapter_uses_restricted_exec_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("app.services.codex_service.subprocess.run", fake_run)

    output_dir = tmp_path / "skills" / "proposed" / "example_skill"
    adapter = RealCodexAdapter(command="codex", timeout_seconds=10, enable_search="false")
    adapter.generate(
        "Generate only this proposed skill.",
        output_dir,
        {"requested_network_domains": ["example.com"], "requested_dependencies": ["requests"]},
    )

    command = captured["command"]
    assert command[0] == "codex"
    assert command.index("--ask-for-approval") < command.index("exec")
    assert command[command.index("-C") + 1] == str(output_dir)
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert command[command.index("--ask-for-approval") + 1] == "never"
    assert "--skip-git-repo-check" in command
    assert "--ephemeral" in command
    assert "--search" not in command
    assert "--output-schema" not in command
    assert command[-1] == "-"
    assert captured["kwargs"]["cwd"] == output_dir
    assert captured["kwargs"]["input"] == "Generate only this proposed skill."
    assert captured["kwargs"]["encoding"] == "utf-8"
    assert captured["kwargs"]["timeout"] == 10
    assert (output_dir / "codex_prompt.txt").is_file()


@pytest.mark.parametrize(
    "action",
    [
        "product_manager_plan_build",
        "product_manager_write_task_dag",
        "product_manager_repair_blueprint",
        "product_manager_update_review",
        "skill_runtime_codex",
    ],
)
def test_machine_consumed_codex_actions_have_valid_output_schemas(action: str) -> None:
    schema = output_schema_for_action(action)

    assert schema is not None
    Draft202012Validator.check_schema(schema)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert all(property_schema.get("description") for property_schema in schema["properties"].values())


@pytest.mark.parametrize(
    ("action", "expected_fields"),
    [
        (
            "product_manager_plan_build",
            {
                "name",
                "description",
                "runtime",
                "requires_invocation_approval",
                "input_schema",
                "output_schema",
                "expected_behavior",
                "functions",
                "schedule",
            },
        ),
        (
            "product_manager_repair_blueprint",
            {
                "goal",
                "skill_name",
                "runtime",
                "requires_invocation_approval",
                "input_schema",
                "output_schema",
                "functions",
                "milestones",
            },
        ),
        (
            "product_manager_update_review",
            {
                "goal",
                "skill_name",
                "runtime",
                "requires_invocation_approval",
                "suggestion",
                "input_schema",
                "output_schema",
                "functions",
                "milestones",
                "permission_plan",
            },
        ),
    ],
)
def test_product_manager_output_schemas_constrain_known_blueprint_fields(
    action: str,
    expected_fields: set[str],
) -> None:
    schema = output_schema_for_action(action)

    assert schema is not None
    blueprint_schema = schema["properties"]["blueprint"]
    if action == "product_manager_plan_build":
        blueprint_schema = blueprint_schema["anyOf"][1]
    assert blueprint_schema["additionalProperties"] is False
    assert set(blueprint_schema["properties"]) == expected_fields
    assert set(blueprint_schema["required"]) == expected_fields - {
        "requires_invocation_approval"
    }


def test_build_blueprint_schema_keeps_nested_callable_schemas_open() -> None:
    schema = output_schema_for_action("product_manager_plan_build")

    assert schema is not None
    blueprint_schema = schema["properties"]["blueprint"]["anyOf"][1]
    validator = Draft202012Validator(blueprint_schema)
    blueprint = {
        "name": "formatter",
        "description": "Build a reusable formatter.",
        "runtime": "function",
        "input_schema": {
            "type": "object",
            "properties": {"user_defined_field": {"type": "string"}},
            "required": ["user_defined_field"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {"another_dynamic_field": {"type": "number"}},
        },
        "expected_behavior": ["Format the supplied value."],
        "functions": [],
        "schedule": None,
    }

    assert validator.is_valid(blueprint)
    assert validator.is_valid({**blueprint, "input_schema": None, "output_schema": None})
    assert validator.is_valid({**blueprint, "unexpected_field": True}) is False
    assert validator.is_valid({key: value for key, value in blueprint.items() if key != "description"}) is False


def test_product_manager_session_schema_confines_blueprint_and_encodes_only_free_form_json() -> None:
    schema = CodexService._product_manager_session_output_schema()

    assert schema is not None
    blueprint_schema = schema["properties"]["blueprint"]["anyOf"][1]
    assert blueprint_schema["type"] == "object"
    assert blueprint_schema["additionalProperties"] is False
    properties = blueprint_schema["properties"]
    assert properties["input_schema"]["type"] == ["string", "null"]
    assert properties["output_schema"]["type"] == ["string", "null"]
    for schedule_variant in properties["schedule"]["anyOf"][1:]:
        assert schedule_variant["properties"]["type"]["type"] == "string"
        assert schedule_variant["properties"]["input"]["type"] == "string"
    assert "uniqueItems" not in json.dumps(schema)


def test_product_manager_session_decodes_only_free_form_blueprint_fields() -> None:
    parsed = {
        "blueprint": {
            "name": "scheduled_formatter",
            "description": "Format a scheduled value.",
            "runtime": "function",
            "input_schema": json.dumps({"type": "object", "properties": {"value": {"type": "string"}}}),
            "output_schema": json.dumps({"type": "object", "properties": {"result": {"type": "string"}}}),
            "expected_behavior": ["Format the supplied value."],
            "functions": [],
            "schedule": {
                "type": "daily",
                "time": "09:00",
                "timezone": "America/Toronto",
                "input": json.dumps({"value": "daily"}),
            },
        }
    }

    decoded = CodexService._decode_product_manager_session_blueprint(parsed)

    blueprint = decoded["blueprint"]
    assert blueprint["name"] == "scheduled_formatter"
    assert blueprint["input_schema"]["properties"]["value"]["type"] == "string"
    assert blueprint["output_schema"]["properties"]["result"]["type"] == "string"
    assert blueprint["schedule"]["input"] == {"value": "daily"}


def test_task_dag_output_schema_uses_the_simplified_node_contract() -> None:
    schema = output_schema_for_action("product_manager_write_task_dag")

    assert schema is not None
    node_schema = schema["properties"]["task_dag"]["properties"]["nodes"]["items"]
    assert node_schema["required"] == [
        "id",
        "task_prompt",
        "depends_on",
        "difficulty",
        "requires_tests",
        "parallel_safe",
        "write_paths",
        "acceptance_criteria",
        "test_expectations",
        "function_ids",
    ]
    assert set(node_schema["properties"]) == set(node_schema["required"])


@pytest.mark.parametrize("action", ["single_codex_build", "skill_build_task", "tester_write_tests", "chat"])
def test_file_and_text_codex_actions_do_not_have_output_schemas(action: str) -> None:
    assert output_schema_for_action(action) is None


def test_real_codex_adapter_passes_and_removes_action_output_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def fake_run(command, **kwargs):
        schema_path = Path(command[command.index("--output-schema") + 1])
        captured["command"] = command
        captured["schema_path"] = schema_path
        captured["schema"] = json.loads(schema_path.read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout='{"explanation":"Expanded.","terms":[],"children":[]}',
            stderr="",
        )

    monkeypatch.setattr("app.services.codex_service.subprocess.run", fake_run)

    output_dir = tmp_path / "runtime" / "product_manager"
    RealCodexAdapter(command="codex", timeout_seconds=10, enable_search="false").generate(
        "Expand this knowledge node.",
        output_dir,
        {"codex_task": "atlas_knowledge_expand"},
    )

    assert "--output-schema" in captured["command"]
    assert captured["schema"]["required"] == ["terms", "children"]
    assert captured["schema_path"].exists() is False


def test_product_manager_session_reuses_thread_and_sends_only_latest_answer(
    tmp_path: Path,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingSession:
        def __init__(self) -> None:
            self.starts: list[dict[str, object]] = []
            self.resumes: list[str] = []
            self.inputs: list[str] = []

        def start_thread(self, **kwargs) -> str:
            self.starts.append(kwargs)
            return "pm-thread-1"

        def resume_thread(self, thread_id: str) -> None:
            self.resumes.append(thread_id)

        def run_structured_turn(
            self,
            thread_id: str,
            input_text: str,
            output_schema: dict[str, object],
            **kwargs,
        ) -> ProductManagerTurnResult:
            self.inputs.append(input_text)
            if len(self.inputs) == 1:
                output = {
                    "decision": "ask_user_for_input",
                    "user_prompt": "Which recurring source should the skill process?",
                    "build_workflow": None,
                    "blueprint": None,
                    "permission_plan": None,
                }
            else:
                blueprint = {
                    "name": "meeting_notes_summary",
                    "description": "Summarize selected Markdown meeting notes.",
                    "runtime": "function",
                    "input_schema": json.dumps({"type": "object", "additionalProperties": True}),
                    "output_schema": json.dumps({"type": "object", "additionalProperties": True}),
                    "expected_behavior": ["Summarize the selected notes."],
                    "functions": [],
                    "schedule": None,
                }
                output = {
                    "decision": "proceed_to_approval",
                    "user_prompt": None,
                    "build_workflow": "single_codex",
                    "blueprint": blueprint,
                    "permission_plan": {
                        "build_time": {"internet_research": False, "dependencies": []},
                        "runtime": {
                            "dependencies": [],
                            "network": [],
                            "codex": {"call_response": False, "internet_access": False},
                        },
                    },
                }
            return ProductManagerTurnResult(
                output_text=json.dumps(output),
                thread_id=thread_id,
                turn_id=f"turn-{len(self.inputs)}",
                usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                model="gpt-test",
                reasoning_effort="high",
                items=[],
                events=[],
            )

    session = RecordingSession()
    monkeypatch.setattr(
        "app.services.product_manager_session_service.product_manager_session_service",
        session,
    )
    generation_request = ChatOrchestrator(db_session).create_generation_request(
        "Build a recurring notes summarizer."
    )
    service = CodexService(
        db_session,
        adapter=RealCodexAdapter(command="codex"),
        project_root=tmp_path,
    )
    resolve_calls = 0

    def resolve(**kwargs) -> ResolvedInvocationSettings:
        nonlocal resolve_calls
        resolve_calls += 1
        return ResolvedInvocationSettings(
            role="product_manager",
            action="product_manager_plan_build",
            route_source="product_manager.blueprint_and_permissions",
            requested_model="gpt-test",
            effective_model="gpt-test",
            requested_reasoning_effort="high",
            effective_reasoning_effort="high",
        )

    monkeypatch.setattr(service.routing_service, "resolve", resolve)
    intent = {"schema_version": 1, "refined_prompt": "Build a recurring notes summarizer."}

    first = service.product_manager_plan_build(generation_request, intent)
    plan = dict(generation_request.plan_json)
    plan["project_conversation"] = [
        *plan["project_conversation"],
        {"role": "assistant", "content": first["user_prompt"]},
        {"role": "user", "content": "Markdown meeting notes selected for each run."},
    ]
    generation_request.plan_json = plan
    db_session.commit()
    second = service.product_manager_plan_build(generation_request, intent)

    assert first["decision"] == "ask_user_for_input"
    assert second["decision"] == "proceed_to_approval"
    assert generation_request.product_manager_thread_id == "pm-thread-1"
    assert resolve_calls == 1
    assert len(session.starts) == 1
    assert session.starts[0]["sandbox"] == "read-only"
    assert session.starts[0]["approval_policy"] == "never"
    assert session.resumes == ["pm-thread-1"]
    assert "function_catalog_index" in session.inputs[0]
    assert "permission_policy" in session.inputs[0]
    assert session.inputs[1].startswith("The user answered your clarification:")
    assert "Markdown meeting notes selected for each run." in session.inputs[1]
    assert "function_catalog_index" not in session.inputs[1]


@pytest.mark.parametrize(
    ("action", "expected_timeout"),
    [
        ("product_manager_plan_build", 180),
        ("product_manager_write_task_dag", 180),
        ("product_manager_repair_blueprint", 180),
        ("product_manager_update_review", 180),
        ("single_codex_build", 900),
        ("skill_generation", 600),
        ("skill_build_task", 600),
        ("skill_repair", 600),
        ("skill_update_repair", 600),
        ("skill_update", 600),
        ("tester_write_tests", 300),
    ],
)
def test_codex_action_timeout_policy(action: str, expected_timeout: int) -> None:
    assert CODEX_ACTION_TIMEOUT_SECONDS[action] == expected_timeout
    assert codex_action_timeout_seconds({"codex_task": action}) == expected_timeout


def test_unknown_codex_action_uses_compatibility_timeout() -> None:
    assert codex_action_timeout_seconds({"codex_task": "legacy_action"}) == DEFAULT_CODEX_ACTION_TIMEOUT_SECONDS


def test_skill_runtime_codex_uses_general_timeout() -> None:
    assert "skill_runtime_codex" not in CODEX_ACTION_TIMEOUT_SECONDS
    assert codex_action_timeout_seconds({"codex_task": "skill_runtime_codex"}) == 300


def test_real_codex_adapter_applies_action_specific_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def fake_run(command, **kwargs):
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("app.services.codex_service.subprocess.run", fake_run)

    RealCodexAdapter(command="codex", enable_search="false").generate(
        "Build and test the complete package.",
        tmp_path / "generated",
        {"codex_task": "single_codex_build"},
    )

    assert captured["kwargs"]["timeout"] == 900


def test_real_codex_adapter_terminates_pre_cancelled_agent_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = 1

        def kill(self) -> None:
            self.returncode = 1

        def communicate(self, input=None, timeout=None):  # noqa: ANN001
            del input, timeout
            return "", ""

    process = FakeProcess()
    monkeypatch.setattr("app.services.codex_service.subprocess.Popen", lambda *args, **kwargs: process)
    agent_run_id = 987654
    codex_process_registry.cancel(agent_run_id)

    with pytest.raises(CodexGenerationError, match="cancelled"):
        RealCodexAdapter(command="codex", timeout_seconds=10, enable_search="false").generate(
            "Build the package.",
            tmp_path / "generated",
            {"codex_task": "single_codex_build", "_agent_run_id": agent_run_id},
        )

    assert process.returncode == 1


def test_product_manager_codex_calls_force_read_only_sandbox(
    tmp_path: Path,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        captured_commands.append(command)
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr("app.services.codex_service.subprocess.run", fake_run)

    plan = skill_plan()
    generation_request = SkillGenerationRequest(
        user_message="Create a reusable local workflow skill.",
        proposed_skill_name=plan["skill_name"],
        proposed_display_name=plan["display_name"],
        plan_json=plan,
        requested_permissions_json=plan["requested_permissions"],
        requested_dependencies_json=plan["requested_dependencies"],
        requested_network_domains_json=plan["requested_network_domains"],
        risk_level=plan["risk_level"],
        status="planned",
    )
    service = CodexService(
        db_session,
        adapter=RealCodexAdapter(command="codex", timeout_seconds=10, sandbox_mode="workspace-write"),
        project_root=tmp_path,
    )

    service.product_manager_write_task_dag(
        generation_request,
        {
            "goal": plan["goal"],
            "skill_name": plan["skill_name"],
            "runtime": "function",
            "input_schema": plan["input_schema"],
            "output_schema": plan["output_schema"],
            "functions": [],
        },
        {"build_time": {}, "runtime": {}},
    )

    invocation_commands = [command for command in captured_commands if "exec" in command]
    assert len(invocation_commands) == 1
    for command in invocation_commands:
        assert command[command.index("-C") + 1] == str(tmp_path / "runtime" / "product_manager")
        assert command[command.index("--sandbox") + 1] == "read-only"


def test_skill_generation_codex_call_forces_workspace_write_inside_skill_folder(
    tmp_path: Path,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("app.services.codex_service.subprocess.run", fake_run)

    plan = skill_plan()
    generation_request = SkillGenerationRequest(
        user_message="Create a reusable local workflow skill.",
        proposed_skill_name=plan["skill_name"],
        proposed_display_name=plan["display_name"],
        plan_json=plan,
        requested_permissions_json=plan["requested_permissions"],
        requested_dependencies_json=plan["requested_dependencies"],
        requested_network_domains_json=plan["requested_network_domains"],
        risk_level=plan["risk_level"],
        status="planned",
    )
    db_session.add(generation_request)
    db_session.commit()
    approve_build_time_permissions(db_session, generation_request)

    CodexService(
        db_session,
        adapter=RealCodexAdapter(command="codex", timeout_seconds=10, sandbox_mode="danger-full-access"),
        project_root=tmp_path,
    ).generate_from_request(generation_request)

    output_dir = tmp_path / "skills" / "proposed" / plan["skill_name"]
    command = captured["command"]
    assert command[command.index("-C") + 1] == str(output_dir)
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert captured["kwargs"]["cwd"] == output_dir


def test_builder_repair_rejects_non_proposed_skill_workspace(tmp_path: Path, db_session: Session) -> None:
    skill = Skill(
        name="installed_skill",
        description="Installed skills are not build-repair workspaces.",
        risk_level="low",
        manifest_path="skills/installed/installed_skill/manifest.json",
        status="installed",
    )
    db_session.add(skill)
    db_session.commit()

    service = CodexService(db_session, adapter=DeterministicCodexStub(), project_root=tmp_path)

    with pytest.raises(CodexGenerationError, match="skills/proposed"):
        service.repair_skill(skill, {"failure_log": "failed"})


def test_builder_repair_restores_tester_owned_files(tmp_path: Path, db_session: Session) -> None:
    skill_dir = tmp_path / "skills" / "proposed" / "guarded_skill"
    tests_dir = skill_dir / "tests"
    tests_dir.mkdir(parents=True)
    existing_test = tests_dir / "test_skill.py"
    existing_test.write_text("def test_original():\n    assert True\n", encoding="utf-8")

    class TestMutatingBuilderAdapter:
        def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
            existing_test.unlink()
            (tests_dir / "test_replacement.py").write_text(
                "def test_replacement():\n    assert False\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(args=["codex"], returncode=0, stdout="ok", stderr="")

    skill = Skill(
        name="guarded_skill",
        description="Exercise Builder ownership enforcement.",
        risk_level="low",
        manifest_path="skills/proposed/guarded_skill/manifest.json",
        status="building",
    )
    db_session.add(skill)
    db_session.commit()

    service = CodexService(db_session, adapter=TestMutatingBuilderAdapter(), project_root=tmp_path)

    with pytest.raises(CodexGenerationError, match="Builder modified Tester-owned files"):
        service.repair_skill(skill, {"failure_log": "failed"})

    assert existing_test.read_text(encoding="utf-8") == "def test_original():\n    assert True\n"
    assert not (tests_dir / "test_replacement.py").exists()


def test_builder_repair_canonicalizes_manifest_permissions(tmp_path: Path, db_session: Session) -> None:
    skill_dir = tmp_path / "skills" / "proposed" / "repair_permissions"
    skill_dir.mkdir(parents=True)
    skill = Skill(
        name="repair_permissions",
        description="Canonicalize repaired manifests.",
        runtime="function",
        risk_level="low",
        manifest_path="skills/proposed/repair_permissions/manifest.json",
        status="building",
        input_schema_json={"type": "object", "additionalProperties": True},
        output_schema_json={"type": "object", "additionalProperties": True},
    )
    db_session.add(skill)
    db_session.commit()

    CodexService(
        db_session,
        adapter=DeterministicCodexStub(),
        project_root=tmp_path,
    ).repair_skill(skill, {"failure_log": "retry"})

    manifest = json.loads((skill_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["permissions"] == {}


def test_real_codex_adapter_auto_enables_search_for_network_plans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("app.services.codex_service.subprocess.run", fake_run)

    RealCodexAdapter(command="codex", timeout_seconds=10, enable_search="auto").generate(
        "Generate a proposed skill.",
        tmp_path / "generated",
        {"requested_network_domains": ["example.com"], "requested_dependencies": []},
    )

    command = captured["command"]
    assert "--search" in command
    assert command.index("--search") < command.index("exec")


def test_real_codex_adapter_auto_search_honors_permission_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("app.services.codex_service.subprocess.run", fake_run)

    RealCodexAdapter(command="codex", timeout_seconds=10, enable_search="auto").generate(
        "Generate a proposed skill.",
        tmp_path / "generated",
        {
            "requested_network_domains": ["wttr.in"],
            "requested_dependencies": ["requests"],
            "permission_plan": {
                "build_time": {
                    "codex_generation": True,
                    "internet_research": False,
                    "dependencies": ["requests"],
                    "reason": "No live research needed.",
                }
            },
        },
    )

    assert "--search" not in captured["command"]


def test_chat_route_returns_400_for_agent_workflow_error(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    def raise_workflow_error(self, *args, **kwargs):
        raise AgentWorkflowError("Proposed skill already exists: generated_skill")

    monkeypatch.setattr("app.services.chat_orchestrator.ChatOrchestrator.handle_message", raise_workflow_error)

    with pytest.raises(HTTPException) as exc_info:
        chat_router.chat(ChatRequest(message="Build a weekly report skill"), db_session)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Proposed skill already exists: generated_skill"


def test_delete_chat_conversation_removes_history_and_clears_memory_source(db_session: Session) -> None:
    deleted_message = Message(role="user", content="Remember my formatter preference.", conversation_id="chat-1")
    kept_message = Message(role="user", content="Keep this message.", conversation_id="chat-2")
    db_session.add_all([deleted_message, kept_message])
    db_session.commit()
    db_session.refresh(deleted_message)
    db_session.refresh(kept_message)
    memory_fact = MemoryFact(
        key="formatter",
        value="Use the repo formatter.",
        category="preferences",
        source_message_id=deleted_message.id,
    )
    db_session.add(memory_fact)
    db_session.commit()

    response = chat_router.delete_chat_conversation("chat-1", db_session)

    assert response.status_code == 204
    assert db_session.query(Message).filter_by(conversation_id="chat-1").count() == 0
    assert db_session.query(Message).filter_by(conversation_id="chat-2").count() == 1
    db_session.refresh(memory_fact)
    assert memory_fact.source_message_id is None


def test_default_codex_mode_uses_real_when_cli_is_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("PERSONAL_AGENT_CODEX_MODE", raising=False)
    executable = tmp_path / "codex.exe"
    executable.touch()
    monkeypatch.setenv("PERSONAL_AGENT_CODEX_COMMAND", str(executable))
    monkeypatch.setattr(
        "app.services.codex_cli_service.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args=args[0], returncode=0, stdout="codex-cli 0.140.0", stderr=""),
    )

    assert isinstance(default_codex_adapter(), RealCodexAdapter)


def test_removed_fake_mode_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONAL_AGENT_CODEX_MODE", "fake")

    adapter = default_codex_adapter()

    assert isinstance(adapter, UnavailableCodexAdapter)
    with pytest.raises(CodexGenerationError, match="fake Codex modes were removed"):
        adapter.generate("prompt", Path.cwd(), {})
