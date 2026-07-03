import json
import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import SkillGenerationRequest
from app.schemas.skill_generation import ChatResponse
from app.services.chat_orchestrator import ChatOrchestrator
from app.services.codex_service import CodexService, FakeCodexAdapter, RealCodexAdapter, default_codex_adapter
from app.services.direct_chat_service import DirectChatService, RealDirectChatAdapter
from app.services.permission_service import PermissionService
from app.services.project_plausibility import (
    FakeProjectPlausibilityAdapter,
    ProjectPlausibilityResult,
    ProjectPlausibilityService,
    RealProjectPlausibilityAdapter,
    default_project_plausibility_adapter,
)
from app.services.proposed_skill_service import ProposedSkillService
from app.services.skill_plan_service import RealSkillPlanAdapter, SkillPlanService


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
        skill_type: str = "automation",
        network: list[str] | None = None,
        shell: bool = False,
    ) -> None:
        self.called = False
        self.skill_type = skill_type
        self.network = network
        self.shell = shell

    def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
        self.called = True
        skill_type = self.skill_type
        permissions = dict(plan["requested_permissions"])
        if skill_type == "instruction":
            permissions = {
                "network": [],
                "filesystem_read": [],
                "filesystem_write": [],
                "secrets": [],
                "shell": False,
            }
        if self.network is not None:
            permissions["network"] = self.network
        if self.shell:
            permissions["shell"] = True
        manifest = {
            "name": plan["skill_name"],
            "description": plan["goal"],
            "skill_type": skill_type,
            "entrypoint": "skill.py" if skill_type in {"automation", "hybrid"} else None,
            "instructions_path": "SKILL.md" if skill_type in {"instruction", "hybrid"} else None,
            "risk_level": "high" if permissions["shell"] else "medium" if permissions["network"] else "low",
            "permissions": permissions,
            "schedule": None,
            "created_by": "codex",
            "enabled": False,
        }
        (output_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (output_dir / "README.md").write_text("# Generated Skill\n", encoding="utf-8")
        if skill_type in {"instruction", "hybrid"}:
            (output_dir / "SKILL.md").write_text("# Instructions\n", encoding="utf-8")
        if skill_type in {"automation", "hybrid"}:
            (output_dir / "skill.py").write_text(
                "from pathlib import Path\n"
                "Path('task_executed.txt').write_text('executed', encoding='utf-8')\n",
                encoding="utf-8",
            )
            tests_dir = output_dir / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_skill.py").write_text("def test_generated():\n    assert True\n", encoding="utf-8")
        return subprocess.CompletedProcess(args=["recording-codex"], returncode=0, stdout="ok", stderr="")


class FixedPlausibilityAdapter:
    def __init__(self, result: ProjectPlausibilityResult) -> None:
        self.result = result
        self.called = False

    def evaluate(self, prompt: str, message: str) -> ProjectPlausibilityResult:
        self.called = True
        return self.result


class FixedSkillPlanAdapter:
    def __init__(self, plan: dict | None = None) -> None:
        self.plan = plan or skill_plan()
        self.called = False
        self.prompt = ""
        self.message = ""

    def build_plan(self, prompt: str, message: str) -> dict:
        self.called = True
        self.prompt = prompt
        self.message = message
        return dict(self.plan)


class RecordingDirectChatAdapter:
    def __init__(self, answer_text: str = "Codex direct answer.") -> None:
        self.called = False
        self.prompt = ""
        self.message = ""
        self.answer_text = answer_text

    def answer(self, prompt: str, message: str) -> str:
        self.called = True
        self.prompt = prompt
        self.message = message
        return self.answer_text


def approve_build_time_permissions(db_session: Session, generation_request: SkillGenerationRequest) -> None:
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
        "skill_type": "automation",
        "interface_type": "chat",
        "files_to_generate": ["manifest.json", "README.md", "skill.py", "tests/test_skill.py"],
        "expected_input": {"input": "object"},
        "expected_output": {"title": "string", "items": [], "warnings": []},
        "input_schema": None,
        "output_schema": None,
        "tool_ui_schema": None,
        "requested_permissions": {
            "network": [],
            "filesystem_read": [],
            "filesystem_write": ["./cache"],
            "secrets": [],
            "shell": False,
        },
        "requested_network_domains": [],
        "requested_dependencies": [],
        "tests_required": True,
        "validation_steps": [
            "validate manifest.json",
            "inspect generated files",
            "run tests for automation or hybrid skills",
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


def test_chat_mode_returns_direct_answer(db_session: Session) -> None:
    adapter = RecordingDirectChatAdapter("Inflation is a broad rise in prices.")
    response = ChatOrchestrator(
        db_session,
        direct_chat_service=DirectChatService(adapter=adapter),
    ).handle_message("What is inflation?", mode="chat")

    assert response["type"] == "direct_answer"
    assert response["message"] == "Inflation is a broad rise in prices."
    assert adapter.called is True
    assert adapter.message == "What is inflation?"
    assert "Chat mode" in adapter.prompt


def test_chat_mode_does_not_create_skill_proposal_from_reusable_request(db_session: Session) -> None:
    adapter = RecordingDirectChatAdapter("Switch to Project mode if you want a reusable skill.")
    response = ChatOrchestrator(
        db_session,
        direct_chat_service=DirectChatService(adapter=adapter),
    ).handle_message(
        "Create a reusable skill that summarizes AI chip news from Nvidia and AMD.",
        mode="chat",
    )

    assert response["type"] == "direct_answer"
    assert adapter.called is True
    assert db_session.query(SkillGenerationRequest).count() == 0


def test_project_mode_creates_skill_proposal(db_session: Session) -> None:
    plausibility_adapter = FixedPlausibilityAdapter(
        ProjectPlausibilityResult(
            plausible=True,
            reason="This is reusable and bounded.",
            optional_projects=[],
        )
    )
    plan_adapter = FixedSkillPlanAdapter(
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
        plausibility_service=ProjectPlausibilityService(adapter=plausibility_adapter),
        skill_plan_service=SkillPlanService(adapter=plan_adapter),
    ).handle_message(
        "Create a reusable skill that summarizes AI chip news from Nvidia and AMD.",
        mode="project",
    )

    assert response["type"] == "skill_generation_plan"
    generation_request = response["generation_request"]
    permission_request = response["permission_request"]
    assert generation_request.status == "awaiting_approval"
    assert generation_request.plan_json["skill_name"] == "ai_infra_news_digest"
    assert generation_request.plan_json["skill_type"] == "automation"
    assert generation_request.plan_json["interface_type"] == "chat"
    assert plan_adapter.called is True
    assert permission_request.request_scope == "build_time"
    assert permission_request.status == "pending"


def test_chat_response_model_serializes_generation_request_fields(db_session: Session) -> None:
    response = ChatOrchestrator(
        db_session,
        skill_plan_service=SkillPlanService(
            adapter=FixedSkillPlanAdapter(
                skill_plan(skill_name="ai_infra_news_digest", display_name="Ai Infra News Digest")
            )
        ),
    ).handle_message(
        "Create a reusable skill that summarizes AI chip news from Nvidia and AMD.",
        mode="project",
    )
    data = TypeAdapter(ChatResponse).validate_python(response).model_dump(mode="json")

    assert data["type"] == "skill_generation_plan"
    assert isinstance(data["generation_request"]["id"], int)
    assert data["generation_request"]["proposed_display_name"] == "Ai Infra News Digest"
    assert isinstance(data["permission_request"]["id"], int)


def test_project_mode_uses_plausibility_review_before_plan(db_session: Session) -> None:
    plausibility_adapter = FixedPlausibilityAdapter(
        ProjectPlausibilityResult(
            plausible=True,
            reason="This is plausible as a reusable skill.",
            optional_projects=[],
        )
    )
    plan_adapter = FixedSkillPlanAdapter()
    response = ChatOrchestrator(
        db_session,
        plausibility_service=ProjectPlausibilityService(adapter=plausibility_adapter),
        skill_plan_service=SkillPlanService(adapter=plan_adapter),
    ).handle_message(
        "Create a reusable skill that summarizes AI chip news from Nvidia and AMD.",
        mode="project",
    )

    generation_request = response["generation_request"]
    assert plausibility_adapter.called is True
    assert plan_adapter.called is True
    assert generation_request.plan_json["plausibility_review"]["reason"] == "This is plausible as a reusable skill."


def test_implausible_project_reports_reason_and_does_not_create_generation_request(db_session: Session) -> None:
    adapter = FixedPlausibilityAdapter(
        ProjectPlausibilityResult(
            plausible=False,
            reason="This is a one-off question, not a reusable project.",
            optional_projects=["Create a reusable instruction for answering this class of question."],
        )
    )
    response = ChatOrchestrator(
        db_session,
        plausibility_service=ProjectPlausibilityService(adapter=adapter),
    ).handle_message("What is inflation?", mode="project")

    assert response == {
        "type": "project_not_plausible",
        "message": "I would not turn that into a skill yet.",
        "reason": "This is a one-off question, not a reusable project.",
        "optional_projects": ["Create a reusable instruction for answering this class of question."],
    }
    assert db_session.query(SkillGenerationRequest).count() == 0


def test_project_mode_does_not_use_backend_unsafe_keyword_heuristic(db_session: Session) -> None:
    response = ChatOrchestrator(db_session).handle_message(
        "Make a skill that deletes files automatically.",
        mode="project",
    )

    assert response["type"] == "skill_generation_plan"
    assert db_session.query(SkillGenerationRequest).count() == 1


def test_generation_request_contains_plan_permissions_and_dependencies(db_session: Session) -> None:
    generation_request = ChatOrchestrator(
        db_session,
        skill_plan_service=SkillPlanService(
            adapter=FixedSkillPlanAdapter(
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
        ),
    ).create_generation_request(
        "Create an automation for tracking Nvidia and AMD news."
    )

    assert generation_request.plan_json["files_to_generate"]
    assert generation_request.requested_permissions_json["network"]
    assert "requests" in generation_request.requested_dependencies_json
    assert generation_request.risk_level == "medium"


def test_build_time_permission_allows_skill_own_cache_read(db_session: Session) -> None:
    response = ChatOrchestrator(
        db_session,
        skill_plan_service=SkillPlanService(
            adapter=FixedSkillPlanAdapter(
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
        ),
    ).handle_message("Create a local game tool with cache-backed state.", mode="project")

    permission_request = response["permission_request"]
    assert permission_request.risk_level == "low"
    assert permission_request.requested_filesystem_json["filesystem_read"] == ["./cache"]


def test_stale_blocked_build_time_request_is_refreshed_before_approval(db_session: Session) -> None:
    response = ChatOrchestrator(
        db_session,
        skill_plan_service=SkillPlanService(
            adapter=FixedSkillPlanAdapter(
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
        ),
    ).handle_message("Create a local game tool with cache-backed state.", mode="project")
    permission_request = response["permission_request"]
    permission_request.risk_level = "blocked"
    db_session.commit()

    approved = PermissionService(db_session).approve_request(permission_request)

    assert approved.status == "approved"
    assert approved.risk_level == "low"


def test_skill_plan_service_uses_adapter_decided_skill_and_interface_type() -> None:
    adapter = FixedSkillPlanAdapter(
        skill_plan(
            skill_name="calculator_tool",
            display_name="Calculator Tool",
            interface_type="tool",
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
            tool_ui_schema={
                "title": "Calculator",
                "description": "Evaluate basic arithmetic.",
                "submit_label": "Calculate",
                "fields": [{"name": "expression", "label": "Expression", "type": "text"}],
                "result_template": {"primary_field": "result", "primary_label": "Result"},
            },
        )
    )

    plan = SkillPlanService(adapter=adapter).build_generation_plan("Build a calculator tool.")

    assert adapter.called is True
    assert plan["skill_type"] == "automation"
    assert plan["interface_type"] == "tool"
    assert plan["skill_name"] == "calculator_tool"
    assert plan["input_schema"]["properties"]["expression"]["type"] == "string"
    assert plan["tool_ui_schema"]["fields"][0]["name"] == "expression"


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


def test_generated_hybrid_validation_runs_tests_but_not_skill_task(
    tmp_path: Path,
    db_session: Session,
) -> None:
    generation_request = ChatOrchestrator(db_session).create_generation_request(
        "Create a reusable instruction and automation workflow skill."
    )
    generation_request.plan_json["skill_type"] = "hybrid"
    approve_build_time_permissions(db_session, generation_request)

    skill, validation = CodexService(
        db_session,
        adapter=RecordingCodexAdapter(skill_type="hybrid"),
        project_root=tmp_path,
    ).generate_from_request(generation_request)

    skill_dir = ProposedSkillService(db_session, project_root=tmp_path).skill_dir_for_record(skill)
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


def test_generated_instruction_skill_does_not_require_tests_and_cannot_run(
    tmp_path: Path,
    db_session: Session,
) -> None:
    generation_request = ChatOrchestrator(db_session).create_generation_request(
        "I want a reusable instruction for how you analyze stocks."
    )
    approve_build_time_permissions(db_session, generation_request)
    skill, validation = CodexService(
        db_session,
        adapter=RecordingCodexAdapter(skill_type="instruction"),
        project_root=tmp_path,
    ).generate_from_request(generation_request)

    assert skill.skill_type == "instruction"
    assert validation.ok is True
    assert validation.tests_run is False

    installed = ProposedSkillService(db_session, project_root=tmp_path).install_proposed_skill(skill)
    assert installed.enabled is True

    from app.services.skill_runner import SkillRunner

    run = SkillRunner(db_session).run(
        skill_id=installed.id,
        skill_dir=ProposedSkillService(db_session, project_root=tmp_path).skill_dir_for_record(installed),
        input_json={},
    )

    assert run.status == "blocked"
    assert run.error_message == "instruction skills cannot be executed"


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
    assert command[-1] == "-"
    assert captured["kwargs"]["cwd"] == output_dir
    assert captured["kwargs"]["input"] == "Generate only this proposed skill."
    assert captured["kwargs"]["encoding"] == "utf-8"
    assert (output_dir / "codex_prompt.txt").is_file()


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


def test_real_project_plausibility_adapter_uses_read_only_codex_exec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=json.dumps({"plausible": True, "reason": "Reusable and bounded.", "optional_projects": []}),
            stderr="",
        )

    monkeypatch.setattr("app.services.project_plausibility.subprocess.run", fake_run)

    adapter = RealProjectPlausibilityAdapter(command="codex", timeout_seconds=10, workdir=tmp_path)
    result = adapter.evaluate("Return JSON only.", "Create a reusable reporting skill.")

    command = captured["command"]
    assert result.plausible is True
    assert command[0] == "codex"
    assert command.index("--ask-for-approval") < command.index("exec")
    assert command[command.index("-C") + 1] == str(tmp_path)
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--ask-for-approval") + 1] == "never"
    assert "--search" not in command
    assert command[-1] == "-"
    assert captured["kwargs"]["cwd"] == tmp_path
    assert captured["kwargs"]["input"] == "Return JSON only."
    assert captured["kwargs"]["encoding"] == "utf-8"


def test_real_direct_chat_adapter_uses_read_only_codex_exec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="Direct Codex answer.", stderr="")

    monkeypatch.setattr("app.services.direct_chat_service.subprocess.run", fake_run)

    adapter = RealDirectChatAdapter(command="codex", timeout_seconds=10, workdir=tmp_path)
    answer = adapter.answer("Answer normally.", "What is inflation?")

    command = captured["command"]
    assert answer == "Direct Codex answer."
    assert command[0] == "codex"
    assert command.index("--ask-for-approval") < command.index("exec")
    assert command[command.index("-C") + 1] == str(tmp_path)
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--ask-for-approval") + 1] == "never"
    assert command[-1] == "-"
    assert captured["kwargs"]["cwd"] == tmp_path
    assert captured["kwargs"]["input"] == "Answer normally."
    assert captured["kwargs"]["encoding"] == "utf-8"


def test_real_skill_plan_adapter_uses_read_only_codex_exec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args=command, returncode=0, stdout=json.dumps(skill_plan()), stderr="")

    monkeypatch.setattr("app.services.skill_plan_service.subprocess.run", fake_run)

    adapter = RealSkillPlanAdapter(command="codex", timeout_seconds=10, workdir=tmp_path)
    plan = adapter.build_plan("Return a plan.", "Build a skill.")

    command = captured["command"]
    assert plan["skill_type"] == "automation"
    assert command[0] == "codex"
    assert command.index("--ask-for-approval") < command.index("exec")
    assert command[command.index("-C") + 1] == str(tmp_path)
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[-1] == "-"
    assert captured["kwargs"]["input"] == "Return a plan."
    assert captured["kwargs"]["encoding"] == "utf-8"


def test_default_codex_mode_uses_real_when_cli_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PERSONAL_AGENT_CODEX_MODE", raising=False)
    monkeypatch.setenv("PERSONAL_AGENT_CODEX_COMMAND", "codex")
    monkeypatch.setattr("app.services.codex_service.shutil.which", lambda command: "C:/Tools/codex.exe")
    monkeypatch.setattr("app.services.project_plausibility.shutil.which", lambda command: "C:/Tools/codex.exe")

    assert isinstance(default_codex_adapter(), RealCodexAdapter)
    assert isinstance(default_project_plausibility_adapter(), RealProjectPlausibilityAdapter)


def test_default_codex_mode_can_be_forced_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONAL_AGENT_CODEX_MODE", "fake")
    monkeypatch.setattr("app.services.codex_service.shutil.which", lambda command: "C:/Tools/codex.exe")
    monkeypatch.setattr("app.services.project_plausibility.shutil.which", lambda command: "C:/Tools/codex.exe")

    assert isinstance(default_codex_adapter(), FakeCodexAdapter)
    assert isinstance(default_project_plausibility_adapter(), FakeProjectPlausibilityAdapter)
