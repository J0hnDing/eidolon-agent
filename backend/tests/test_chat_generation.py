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
from app.services.codex_service import CodexService
from app.services.permission_service import PermissionService
from app.services.project_plausibility import ProjectPlausibilityResult, ProjectPlausibilityService
from app.services.proposed_skill_service import ProposedSkillService


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


def approve_build_time_permissions(db_session: Session, generation_request: SkillGenerationRequest) -> None:
    permission_service = PermissionService(db_session)
    request = permission_service.create_build_time_request(generation_request)
    permission_service.approve_request(request)
    generation_request.status = "approved"
    db_session.commit()


def test_chat_mode_returns_direct_answer(db_session: Session) -> None:
    response = ChatOrchestrator(db_session).handle_message("What is inflation?", mode="chat")

    assert response["type"] == "direct_answer"


def test_chat_mode_does_not_create_skill_proposal_from_reusable_request(db_session: Session) -> None:
    response = ChatOrchestrator(db_session).handle_message(
        "Create a reusable skill that summarizes AI chip news from Nvidia and AMD.",
        mode="chat",
    )

    assert response["type"] == "direct_answer"


def test_project_mode_creates_skill_proposal(db_session: Session) -> None:
    adapter = FixedPlausibilityAdapter(
        ProjectPlausibilityResult(
            plausible=True,
            reason="This is reusable and bounded.",
            optional_projects=[],
        )
    )
    response = ChatOrchestrator(
        db_session,
        plausibility_service=ProjectPlausibilityService(adapter=adapter),
    ).handle_message(
        "Create a reusable skill that summarizes AI chip news from Nvidia and AMD.",
        mode="project",
    )

    assert response["type"] == "skill_generation_plan"
    generation_request = response["generation_request"]
    permission_request = response["permission_request"]
    assert generation_request.status == "awaiting_approval"
    assert generation_request.plan_json["skill_name"] == "ai_infra_news_digest"
    assert permission_request.request_scope == "build_time"
    assert permission_request.status == "pending"


def test_chat_response_model_serializes_generation_request_fields(db_session: Session) -> None:
    response = ChatOrchestrator(db_session).handle_message(
        "Create a reusable skill that summarizes AI chip news from Nvidia and AMD.",
        mode="project",
    )
    data = TypeAdapter(ChatResponse).validate_python(response).model_dump(mode="json")

    assert data["type"] == "skill_generation_plan"
    assert isinstance(data["generation_request"]["id"], int)
    assert data["generation_request"]["proposed_display_name"] == "Ai Infra News Digest"
    assert isinstance(data["permission_request"]["id"], int)


def test_project_mode_uses_plausibility_review_before_plan(db_session: Session) -> None:
    adapter = FixedPlausibilityAdapter(
        ProjectPlausibilityResult(
            plausible=True,
            reason="This is plausible as a reusable skill.",
            optional_projects=[],
        )
    )
    response = ChatOrchestrator(
        db_session,
        plausibility_service=ProjectPlausibilityService(adapter=adapter),
    ).handle_message(
        "Create a reusable skill that summarizes AI chip news from Nvidia and AMD.",
        mode="project",
    )

    generation_request = response["generation_request"]
    assert adapter.called is True
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


def test_unsafe_chat_request_is_rejected(db_session: Session) -> None:
    response = ChatOrchestrator(db_session).handle_message(
        "Make a skill that deletes files automatically.",
        mode="project",
    )

    assert response["type"] == "unsafe_or_unsupported"


def test_generation_request_contains_plan_permissions_and_dependencies(db_session: Session) -> None:
    generation_request = ChatOrchestrator(db_session).create_generation_request(
        "Create an automation for tracking Nvidia and AMD news."
    )

    assert generation_request.plan_json["files_to_generate"]
    assert generation_request.requested_permissions_json["network"]
    assert "requests" in generation_request.requested_dependencies_json
    assert generation_request.risk_level == "medium"


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


def test_run_decision_blocks_approved_but_unsupported_network(
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
    assert decision.allowed is False
    assert "current runner cannot enforce domain-level network sandboxing" in decision.reason


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


def test_network_requesting_generated_skill_is_not_runnable_under_current_runner(
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
        "This skill requests network access, but the current runner does not support networked execution yet."
    ]

    installed = ProposedSkillService(db_session, project_root=tmp_path).install_proposed_skill(skill)
    installed.enabled = True
    db_session.commit()

    from app.services.skill_runner import SkillRunner

    run = SkillRunner(db_session).run(
        skill_id=installed.id,
        skill_dir=ProposedSkillService(db_session, project_root=tmp_path).skill_dir_for_record(installed),
        input_json={},
    )

    assert run.status == "blocked"
    assert run.error_message == "network permissions are not supported in Milestone 3"


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
