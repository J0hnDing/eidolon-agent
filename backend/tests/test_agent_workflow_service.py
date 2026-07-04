import json
import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import AgentRunStep, Skill, SkillGenerationRequest, SkillRun
from app.services.agent_workflow_service import AgentWorkflowError, AgentWorkflowService
from app.services.chat_orchestrator import ChatOrchestrator
from app.services.codex_service import CodexService, FakeCodexAdapter
from app.services.permission_service import PermissionService
from app.routers.agent_runs import delete_agent_run


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


def test_product_manager_creates_blueprint_and_milestones(db_session: Session) -> None:
    response = ChatOrchestrator(db_session).handle_message("Create a reusable local workflow skill.", mode="project")

    agent_run = AgentWorkflowService(db_session).latest_run_for_generation(response["generation_request"].id)
    building_skill = db_session.get(Skill, response["generation_request"].proposed_skill_id)

    assert agent_run.status == "waiting_for_approval"
    assert agent_run.current_step == "product_manager"
    assert agent_run.current_milestone == "core_skill"
    assert building_skill.status == "building"
    assert agent_run.blueprint_json["milestones"][0]["name"] == "core_skill"
    steps = sorted(agent_run.steps, key=lambda step: step.id)
    assert [step.step_name for step in steps] == ["product_manager", "product_manager"]
    assert steps[0].output_json["decision_json"]["decision"] == "build_next_milestone"
    assert steps[1].output_json["decision_json"]["decision"] == "request_permission"
    assert steps[1].output_json["blueprint_json"]["skill_name"] == building_skill.name
    assert steps[1].output_json["permission_path"].endswith("permissions.json")
    assert steps[1].output_json["milestone_paths"][0].endswith("milestones/core_skill.json")


def test_product_manager_uses_codex_adapter_for_blueprint_and_summary(tmp_path: Path, db_session: Session) -> None:
    generation_request = create_generation_request(db_session)

    class RecordingAdapter(FakeCodexAdapter):
        def __init__(self) -> None:
            self.tasks: list[str] = []
            self.plans: list[dict] = []

        def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
            task = plan.get("codex_task")
            if task:
                self.tasks.append(task)
                self.plans.append(dict(plan))
            return super().generate(prompt, output_dir, plan)

    adapter = RecordingAdapter()
    agent_run = AgentWorkflowService(
        db_session,
        codex_service=CodexService(db_session, adapter=adapter, project_root=tmp_path),
        project_root=tmp_path,
    ).create_build_run(generation_request)

    assert "product_manager_build_review" in adapter.tasks
    assert "product_manager_build_blueprint" in adapter.tasks
    assert "product_manager_summary" in adapter.tasks
    assert "product_manager_summary" not in agent_run.blueprint_json
    build_summary_plan = next(
        plan
        for plan in adapter.plans
        if plan.get("codex_task") == "product_manager_summary" and plan.get("summary_type") == "build_time"
    )
    context = build_summary_plan["context"]
    assert context["user_request"] == generation_request.user_message
    assert context["permission_plan"]["runtime"]["permissions"]["shell"] is False
    assert context["blueprint_path"].endswith("blueprint.json")
    assert context["approval_boundary"]["approval_means"] == "Codex may generate proposed skill files only."


def test_approval_updates_waiting_product_manager_permission_step(db_session: Session) -> None:
    response = ChatOrchestrator(db_session).handle_message("Create a reusable local workflow skill.", mode="project")
    permission_request = response["permission_request"]

    PermissionService(db_session).approve_request(permission_request)

    agent_run = AgentWorkflowService(db_session).latest_run_for_generation(response["generation_request"].id)
    db_session.refresh(response["generation_request"])
    pm_step = [step for step in agent_run.steps if (step.output_json or {}).get("permission_request_id")][0]
    assert pm_step.status == "succeeded"
    assert pm_step.output_json["status"] == "approved"
    assert agent_run.status == "pending"
    assert response["generation_request"].status == "approved"


def test_resume_after_generic_approval_runs_builder(tmp_path: Path, db_session: Session) -> None:
    generation_request = create_generation_request(db_session)
    agent_run = AgentWorkflowService(db_session, project_root=tmp_path).create_build_run(generation_request)
    permission_request = PermissionService(db_session, project_root=tmp_path).create_build_time_request(generation_request)
    PermissionService(db_session, project_root=tmp_path).approve_request(permission_request)

    resumed = AgentWorkflowService(
        db_session,
        codex_service=CodexService(db_session, adapter=FakeCodexAdapter(), project_root=tmp_path),
        project_root=tmp_path,
    ).resume_run(agent_run)

    assert resumed.status == "succeeded"
    assert [step.step_name for step in resumed.steps].count("builder") == 1
    assert all(step.error_message != "Generation request is not approved for generation" for step in resumed.steps)


def test_build_time_permission_summary_has_pm_and_permission_review_parts(db_session: Session) -> None:
    response = ChatOrchestrator(db_session).handle_message("Create a reusable local workflow skill.", mode="project")
    permission_request = response["permission_request"]

    assert "ProductManager:" in permission_request.user_explanation
    assert "Permission review:" in permission_request.user_explanation
    assert permission_request.reason_json["product_manager_summary"]
    assert permission_request.reason_json["permission_review_summary"]


def test_workflow_pauses_for_single_user_approval_before_build(tmp_path: Path, db_session: Session) -> None:
    generation_request = create_generation_request(db_session)
    agent_run = AgentWorkflowService(db_session, project_root=tmp_path).create_build_run(generation_request)

    with pytest.raises(AgentWorkflowError, match="Build-time permission"):
        AgentWorkflowService(db_session, project_root=tmp_path).continue_build_after_approval(generation_request)

    assert agent_run.status == "waiting_for_approval"
    assert not (tmp_path / "skills" / "proposed").exists()


def test_approved_build_uses_pm_builder_tester_and_permission_artifacts(tmp_path: Path, db_session: Session) -> None:
    generation_request = create_generation_request(db_session)
    AgentWorkflowService(db_session, project_root=tmp_path).create_build_run(generation_request)
    approve_generation(db_session, generation_request, tmp_path)
    adapter = FakeCodexAdapter()

    agent_run, skill, validation = AgentWorkflowService(
        db_session,
        codex_service=CodexService(db_session, adapter=adapter, project_root=tmp_path),
        project_root=tmp_path,
    ).continue_build_after_approval(generation_request)

    assert validation.ok is True
    assert skill.status == "proposed"
    assert skill.installed_path is None
    assert db_session.query(SkillRun).count() == 0
    assert agent_run.status == "succeeded"
    assert set(step.step_name for step in agent_run.steps) == {
        "product_manager",
        "builder",
        "tester",
    }
    approval_pm_step = [step for step in agent_run.steps if (step.output_json or {}).get("permission_request_id")][0]
    assert approval_pm_step.status == "succeeded"
    assert "Approved by local user" in approval_pm_step.logs
    runtime_artifact = tmp_path / "runtime" / "agent_runs" / f"run_{agent_run.id}" / "runtime_permissions.json"
    assert runtime_artifact.is_file()
    assert json.loads(runtime_artifact.read_text(encoding="utf-8"))["skill_id"] == skill.id
    tester_step = [step for step in agent_run.steps if step.step_name == "tester"][0]
    assert "skill.py" in tester_step.input_json["code_files"]
    assert tester_step.output_json["tests_written"] == ["tests/test_skill.py"]
    assert tester_step.output_json["tester_generation"]["stdout"] == "fake tester wrote tests"
    test_file = tmp_path / "skills" / "proposed" / skill.name / "tests" / "test_skill.py"
    test_source = test_file.read_text(encoding="utf-8")
    assert "test_manifest_matches_blueprint_and_safe_contract" in test_source
    assert "test_skill_accepts_representative_input_and_outputs_json_object" in test_source


def test_build_workflow_executes_pm_milestone_files_in_order(tmp_path: Path, db_session: Session) -> None:
    generation_request = create_generation_request(db_session)

    class TwoMilestoneAdapter(FakeCodexAdapter):
        def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
            result = super().generate(prompt, output_dir, plan)
            if plan.get("codex_task") == "product_manager_build_blueprint":
                payload = json.loads(result.stdout)
                payload["blueprint"]["milestones"] = [
                    {
                        "name": "scaffold",
                        "summary": "Create the basic skill package files.",
                        "acceptance_criteria": ["manifest.json is valid", "skill.py exists"],
                    },
                    {
                        "name": "edge_cases",
                        "summary": "Add input edge case behavior.",
                        "acceptance_criteria": ["empty input returns JSON", "tests pass"],
                    },
                ]
                return subprocess.CompletedProcess(
                    args=result.args,
                    returncode=0,
                    stdout=json.dumps(payload),
                    stderr="",
                )
            return result

    adapter = TwoMilestoneAdapter()
    service = AgentWorkflowService(
        db_session,
        codex_service=CodexService(db_session, adapter=adapter, project_root=tmp_path),
        project_root=tmp_path,
    )
    agent_run = service.create_build_run(generation_request)
    approve_generation(db_session, generation_request, tmp_path)

    agent_run, _skill, validation = service.continue_build_after_approval(generation_request)

    assert validation.ok is True
    assert agent_run.status == "succeeded"
    assert agent_run.failure_count_json == {"scaffold": 0, "edge_cases": 0}
    assert [step.milestone_name for step in agent_run.steps if step.step_name == "builder"] == ["scaffold", "edge_cases"]
    assert [step.milestone_name for step in agent_run.steps if step.step_name == "tester"] == ["scaffold", "edge_cases"]
    scaffold_file = tmp_path / "runtime" / "agent_runs" / f"run_{agent_run.id}" / "milestones" / "scaffold.json"
    edge_file = tmp_path / "runtime" / "agent_runs" / f"run_{agent_run.id}" / "milestones" / "edge_cases.json"
    assert scaffold_file.is_file()
    assert edge_file.is_file()
    scaffold_payload = json.loads(scaffold_file.read_text(encoding="utf-8"))
    assert "blueprint_path" not in scaffold_payload
    assert "permission_path" not in scaffold_payload
    builder_steps = [step for step in agent_run.steps if step.step_name == "builder"]
    assert builder_steps[0].input_json["milestone_path"].endswith("milestones/scaffold.json")
    assert builder_steps[1].input_json["milestone_path"].endswith("milestones/edge_cases.json")


def test_tester_agent_invokes_codex_with_blueprint_and_code_context(tmp_path: Path, db_session: Session) -> None:
    generation_request = create_generation_request(db_session)
    AgentWorkflowService(db_session, project_root=tmp_path).create_build_run(generation_request)
    approve_generation(db_session, generation_request, tmp_path)

    class RecordingAdapter(FakeCodexAdapter):
        def __init__(self) -> None:
            self.plans: list[dict] = []
            self.prompts: list[str] = []

        def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
            self.prompts.append(prompt)
            self.plans.append(dict(plan))
            return super().generate(prompt, output_dir, plan)

    adapter = RecordingAdapter()
    agent_run, skill, validation = AgentWorkflowService(
        db_session,
        codex_service=CodexService(db_session, adapter=adapter, project_root=tmp_path),
        project_root=tmp_path,
    ).continue_build_after_approval(generation_request)

    assert validation.ok is True
    tester_plans = [plan for plan in adapter.plans if plan.get("codex_task") == "tester_write_tests"]
    assert len(tester_plans) == 1
    tester_plan = tester_plans[0]
    assert tester_plan["blueprint_json"]["skill_name"] == skill.name
    assert "skill.py" in tester_plan["code_files"]
    assert any("You are TesterAgent" in prompt for prompt in adapter.prompts)
    assert any("Write only this file: tests/test_skill.py" in prompt for prompt in adapter.prompts)
    builder_plans = [plan for plan in adapter.plans if plan.get("codex_task") != "tester_write_tests"]
    assert builder_plans[0]["builder_writes_tests"] is False
    assert agent_run.status == "succeeded"


def test_failed_test_triggers_builder_repair(tmp_path: Path, db_session: Session) -> None:
    generation_request = create_generation_request(db_session)
    AgentWorkflowService(db_session, project_root=tmp_path).create_build_run(generation_request)
    approve_generation(db_session, generation_request, tmp_path)

    class RepairingAdapter(FakeCodexAdapter):
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
            if plan.get("codex_task", "").startswith("product_manager"):
                return super().generate(prompt, output_dir, plan)
            self.calls += 1
            result = super().generate(prompt, output_dir, plan)
            if self.calls == 1:
                (output_dir / "skill.py").write_text(
                    "import sys\n\n"
                    "def main():\n"
                    "    sys.exit(1)\n\n"
                    "if __name__ == '__main__':\n"
                    "    main()\n",
                    encoding="utf-8",
                )
            return result

    adapter = RepairingAdapter()
    agent_run, _skill, validation = AgentWorkflowService(
        db_session,
        codex_service=CodexService(db_session, adapter=adapter, project_root=tmp_path),
        project_root=tmp_path,
    ).continue_build_after_approval(generation_request)

    assert adapter.calls == 4
    assert validation.ok is True
    assert agent_run.failure_count_json["core_skill"] == 1
    builder_steps = [step for step in agent_run.steps if step.step_name == "builder"]
    assert builder_steps[0].input_json["mode"] == "build"
    assert builder_steps[1].input_json["mode"] == "repair"
    tester_steps = [step for step in agent_run.steps if step.step_name == "tester"]
    assert tester_steps[0].status == "failed"
    assert tester_steps[-1].status == "succeeded"


def test_more_than_three_failures_stops_workflow(tmp_path: Path, db_session: Session) -> None:
    generation_request = create_generation_request(db_session)
    AgentWorkflowService(db_session, project_root=tmp_path).create_build_run(generation_request)
    approve_generation(db_session, generation_request, tmp_path)

    class AlwaysFailingTestsAdapter(FakeCodexAdapter):
        def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
            result = super().generate(prompt, output_dir, plan)
            (output_dir / "skill.py").write_text(
                "import sys\n\n"
                "def main():\n"
                "    sys.exit(1)\n\n"
                "if __name__ == '__main__':\n"
                "    main()\n",
                encoding="utf-8",
            )
            return result

    agent_run, skill, validation = AgentWorkflowService(
        db_session,
        codex_service=CodexService(db_session, adapter=AlwaysFailingTestsAdapter(), project_root=tmp_path),
        project_root=tmp_path,
    ).continue_build_after_approval(generation_request)

    assert validation.ok is False
    assert agent_run.status == "failed"
    assert agent_run.failure_count_json["core_skill"] == 4
    assert agent_run.final_summary_json["failure_count_json"]["core_skill"] == 4
    assert skill.status == "failed"
    product_manager_steps = [step for step in agent_run.steps if step.step_name == "product_manager"]
    assert product_manager_steps[-1].output_json["decision_json"]["decision"] == "stop_failed"


def test_builder_user_action_required_blocks_workflow(tmp_path: Path, db_session: Session) -> None:
    generation_request = create_generation_request(db_session)
    AgentWorkflowService(db_session, project_root=tmp_path).create_build_run(generation_request)
    approve_generation(db_session, generation_request, tmp_path)

    class UserActionAdapter(FakeCodexAdapter):
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
            self.calls += 1
            if self.calls == 1:
                result = super().generate(prompt, output_dir, plan)
                (output_dir / "skill.py").write_text(
                    "import sys\n\n"
                    "def main():\n"
                    "    sys.exit(1)\n\n"
                    "if __name__ == '__main__':\n"
                    "    main()\n",
                    encoding="utf-8",
                )
                return result
            return subprocess.CompletedProcess(
                args=["fake"],
                returncode=1,
                stdout="",
                stderr="USER_ACTION_REQUIRED: missing API key",
            )

    with pytest.raises(AgentWorkflowError, match="missing API key"):
        AgentWorkflowService(
            db_session,
            codex_service=CodexService(db_session, adapter=UserActionAdapter(), project_root=tmp_path),
            project_root=tmp_path,
        ).continue_build_after_approval(generation_request)

    agent_run = AgentWorkflowService(db_session, project_root=tmp_path).latest_run_for_generation(generation_request.id)
    assert agent_run.status == "blocked"
    assert agent_run.final_summary_json["user_action_required"]["exact_blocker"] == "missing API key"


def test_permission_review_records_permission_expansion(tmp_path: Path, db_session: Session) -> None:
    generation_request = create_generation_request(db_session)
    AgentWorkflowService(db_session, project_root=tmp_path).create_build_run(generation_request)
    approve_generation(db_session, generation_request, tmp_path)

    class ExpandedPermissionAdapter(FakeCodexAdapter):
        def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
            result = super().generate(prompt, output_dir, plan)
            if plan.get("codex_task", "").startswith("product_manager"):
                return result
            manifest_path = output_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["permissions"]["network"] = ["example.com"]
            manifest["risk_level"] = "medium"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            return result

    agent_run, _skill, _validation = AgentWorkflowService(
        db_session,
        codex_service=CodexService(db_session, adapter=ExpandedPermissionAdapter(), project_root=tmp_path),
        project_root=tmp_path,
    ).continue_build_after_approval(generation_request)

    runtime_artifact = tmp_path / "runtime" / "agent_runs" / f"run_{agent_run.id}" / "runtime_permissions.json"
    runtime_review = json.loads(runtime_artifact.read_text(encoding="utf-8"))
    assert runtime_review["permission_expansion"] == {"network": ["example.com"]}


def test_repair_agent_run_creates_proposed_copy_for_installed_skill(tmp_path: Path, db_session: Session) -> None:
    installed_skill = create_installed_skill(tmp_path, db_session, "broken_tool")

    agent_run = AgentWorkflowService(
        db_session,
        codex_service=CodexService(db_session, adapter=FakeCodexAdapter(), project_root=tmp_path),
        project_root=tmp_path,
    ).create_repair_run(installed_skill)

    assert agent_run.run_type == "repair_skill"
    assert agent_run.status == "succeeded"
    assert agent_run.skill_id != installed_skill.id
    repair_skill = db_session.get(Skill, agent_run.skill_id)
    assert repair_skill.status == "proposed"
    assert repair_skill.installed_path is None
    assert db_session.query(SkillRun).count() == 0


def test_cancelled_agent_run_blocks_further_steps(tmp_path: Path, db_session: Session) -> None:
    generation_request = create_generation_request(db_session)
    service = AgentWorkflowService(db_session, project_root=tmp_path)
    agent_run = service.create_build_run(generation_request)
    service.cancel_run(agent_run)
    approve_generation(db_session, generation_request, tmp_path)

    with pytest.raises(AgentWorkflowError, match="cancelled"):
        service.continue_build_after_approval(generation_request)


def test_delete_agent_run_removes_steps(db_session: Session) -> None:
    response = ChatOrchestrator(db_session).handle_message("Create a reusable local workflow skill.", mode="project")
    agent_run = AgentWorkflowService(db_session).latest_run_for_generation(response["generation_request"].id)
    step_ids = [step.id for step in agent_run.steps]

    delete_agent_run(agent_run.id, db_session)

    assert db_session.get(type(agent_run), agent_run.id) is None
    assert all(db_session.get(AgentRunStep, step_id) is None for step_id in step_ids)


def create_generation_request(db: Session) -> SkillGenerationRequest:
    return ChatOrchestrator(db).create_generation_request("Create a reusable local workflow skill.")


def approve_generation(db: Session, generation_request: SkillGenerationRequest, project_root: Path) -> None:
    permission_service = PermissionService(db, project_root=project_root)
    request = permission_service.create_build_time_request(generation_request)
    permission_service.approve_request(request)
    generation_request.status = "approved"
    db.commit()


def create_installed_skill(tmp_path: Path, db: Session, name: str) -> Skill:
    skill_dir = tmp_path / "skills" / "installed" / name
    tests_dir = skill_dir / "tests"
    tests_dir.mkdir(parents=True)
    manifest = {
        "name": name,
        "description": "Broken skill for repair tests.",
        "skill_type": "automation",
        "interface_type": "chat",
        "entrypoint": "skill.py",
        "instructions_path": None,
        "input_schema": None,
        "output_schema": None,
        "tool_ui_schema": None,
        "risk_level": "low",
        "permissions": {
            "network": [],
            "filesystem_read": [],
            "filesystem_write": ["./cache"],
            "secrets": [],
            "shell": False,
        },
        "schedule": None,
        "created_by": "test",
        "enabled": True,
    }
    (skill_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (skill_dir / "README.md").write_text("# Broken skill\n", encoding="utf-8")
    (skill_dir / "skill.py").write_text("import json\nprint(json.dumps({'ok': True}))\n", encoding="utf-8")
    (tests_dir / "test_skill.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    skill = Skill(
        name=name,
        description=manifest["description"],
        skill_type="automation",
        interface_type="chat",
        status="installed",
        risk_level="low",
        manifest_path=f"skills/installed/{name}/manifest.json",
        installed_path=f"skills/installed/{name}",
        enabled=True,
    )
    db.add(skill)
    db.commit()
    db.refresh(skill)
    return skill
