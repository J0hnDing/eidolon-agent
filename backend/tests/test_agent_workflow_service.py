import json
import subprocess
import copy
from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import AgentRunStep, MemoryFact, Skill, SkillGenerationRequest, SkillRun
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


def test_product_manager_creates_blueprint_and_permissions_before_task_dag(tmp_path: Path, db_session: Session) -> None:
    generation_request = create_generation_request(db_session)
    agent_run = AgentWorkflowService(db_session, project_root=tmp_path).create_build_run(generation_request)

    building_skill = db_session.get(Skill, generation_request.proposed_skill_id)
    run_dir = tmp_path / "runtime" / "agent_runs" / f"run_{agent_run.id}"

    assert agent_run.status == "waiting_for_approval"
    assert agent_run.current_step == "product_manager"
    assert agent_run.current_task_id is None
    assert building_skill.status == "building"
    assert "milestones" not in agent_run.blueprint_json
    steps = sorted(agent_run.steps, key=lambda step: step.id)
    assert [step.step_name for step in steps] == ["product_manager"] * 5
    assert [step.input_json["action"] for step in steps] == [
        "pm_refine_intent",
        "pm_review_plausibility",
        "pm_write_blueprint",
        "pm_write_permissions",
        "backend_build_time_permission_review",
    ]
    assert steps[0].output_json["intent_prompt_path"].endswith("intent_prompt.json")
    assert steps[1].output_json["decision_json"]["decision"] == "proceed_to_blueprint"
    assert steps[2].output_json["blueprint_json"]["skill_name"] == building_skill.name
    assert steps[3].output_json["permission_path"].endswith("permissions.json")
    assert steps[4].output_json["decision_json"]["decision"] == "request_permission"
    assert "task_dag_path" not in steps[4].output_json
    assert (run_dir / "intent_prompt.json").is_file()
    assert (run_dir / "decision.json").is_file()
    assert (run_dir / "blueprint.json").is_file()
    assert (run_dir / "permissions.json").is_file()
    assert not (run_dir / "task_dag.json").exists()
    assert not (run_dir / "tasks").exists()
    assert all(not path.startswith("tests/") for path in agent_run.blueprint_json["expected_files"])


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

    selected_fact = MemoryFact(
        key="preferred_output_style",
        value="Keep generated skills local-first.",
        category="preference",
        sensitivity="normal",
    )
    db_session.add(selected_fact)
    db_session.add(
        MemoryFact(
            key="private_fact",
            value="Do not include this.",
            category="preference",
            sensitivity="normal",
            user_editable=False,
        )
    )
    db_session.commit()

    adapter = RecordingAdapter()
    AgentWorkflowService(
        db_session,
        codex_service=CodexService(db_session, adapter=adapter, project_root=tmp_path),
        project_root=tmp_path,
    ).create_build_run(generation_request)

    assert "product_manager_build_review" in adapter.tasks
    assert "product_manager_refine_intent" in adapter.tasks
    assert "product_manager_write_blueprint" in adapter.tasks
    assert "product_manager_write_permissions" in adapter.tasks
    assert "product_manager_summary" in adapter.tasks
    refine_plan = next(plan for plan in adapter.plans if plan.get("codex_task") == "product_manager_refine_intent")
    assert refine_plan["selected_memory_facts"] == [
        {
            "id": selected_fact.id,
            "key": "preferred_output_style",
            "value": "Keep generated skills local-first.",
            "category": "preference",
            "sensitivity": "normal",
        }
    ]


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
    assert "tests/test_core_skill.py" in tester_step.output_json["tests_written"]
    assert tester_step.output_json["tester_generation"]["stdout"] == "fake tester wrote tests"
    task_dag_artifact = tmp_path / "runtime" / "agent_runs" / f"run_{agent_run.id}" / "task_dag.json"
    task_artifact = tmp_path / "runtime" / "agent_runs" / f"run_{agent_run.id}" / "tasks" / "core_skill.json"
    interface_artifact = tmp_path / "runtime" / "agent_runs" / f"run_{agent_run.id}" / "tasks" / "core_skill" / "interface_artifact.json"
    assert task_dag_artifact.is_file()
    assert task_artifact.is_file()
    assert interface_artifact.is_file()
    test_file = tmp_path / "skills" / "proposed" / skill.name / "tests" / "test_core_skill.py"
    test_source = test_file.read_text(encoding="utf-8")
    assert "test_manifest_matches_blueprint_and_safe_contract" in test_source
    assert "test_skill_accepts_representative_input_and_outputs_json_object" in test_source


def test_backend_seeds_and_finalizes_manifest_json(tmp_path: Path, db_session: Session) -> None:
    generation_request = create_generation_request(db_session)
    AgentWorkflowService(db_session, project_root=tmp_path).create_build_run(generation_request)
    approve_generation(db_session, generation_request, tmp_path)

    class ManifestAdapter(FakeCodexAdapter):
        def __init__(self) -> None:
            self.seeded_manifest: dict | None = None

        def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
            if plan.get("current_milestone") and not plan.get("codex_task"):
                self.seeded_manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
                result = super().generate(prompt, output_dir, plan)
                manifest_path = output_dir / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                for key in ("risk_level", "permissions", "schedule", "dependencies", "created_by", "enabled"):
                    manifest.pop(key, None)
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                return result
            return super().generate(prompt, output_dir, plan)

    adapter = ManifestAdapter()
    _agent_run, skill, validation = AgentWorkflowService(
        db_session,
        codex_service=CodexService(db_session, adapter=adapter, project_root=tmp_path),
        project_root=tmp_path,
    ).continue_build_after_approval(generation_request)

    assert validation.ok is True
    assert adapter.seeded_manifest is not None
    assert adapter.seeded_manifest["name"] == skill.name
    assert adapter.seeded_manifest["permissions"]["shell"] is False
    final_manifest = json.loads(
        (tmp_path / "skills" / "proposed" / skill.name / "manifest.json").read_text(encoding="utf-8")
    )
    assert final_manifest["risk_level"] == generation_request.plan_json["risk_level"]
    assert final_manifest["permissions"]["shell"] is False
    assert final_manifest["schedule"] is None
    assert final_manifest["dependencies"] == generation_request.plan_json["requested_dependencies"]
    assert final_manifest["created_by"] == "codex"
    assert final_manifest["enabled"] is False


def test_build_workflow_executes_pm_task_dag_in_dependency_order(tmp_path: Path, db_session: Session) -> None:
    generation_request = create_generation_request(db_session)

    class TwoTaskAdapter(FakeCodexAdapter):
        def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
            result = super().generate(prompt, output_dir, plan)
            if plan.get("codex_task") == "product_manager_write_task_dag":
                payload = {
                    "task_dag": {
                        "schema_version": 1,
                        "graph_id": "generated_skill_build",
                        "root_task_ids": ["scaffold"],
                        "nodes": [
                    {
                        "id": "scaffold",
                        "title": "Scaffold",
                        "summary": "Create the basic skill package files.",
                                "depends_on": [],
                                "difficulty": "easy",
                                "requires_tests": True,
                                "parallel_safe": True,
                                "expected_inputs": ["blueprint.json", "permissions.json"],
                                "parent_interface_artifacts": [],
                                "expected_output_paths": ["manifest.json", "README.md", "skill.py"],
                                "file_write_claims": ["manifest.json", "README.md", "skill.py"],
                        "acceptance_criteria": ["manifest.json is valid", "skill.py exists"],
                                "test_expectations": ["manifest validates"],
                                "interface_artifact_expectations": ["declare skill.py entrypoint"],
                    },
                    {
                        "id": "edge_cases",
                        "title": "Edge cases",
                        "summary": "Add input edge case behavior.",
                                "depends_on": ["scaffold"],
                                "difficulty": "medium",
                                "requires_tests": True,
                                "parallel_safe": True,
                                "expected_inputs": ["tasks/scaffold/interface_artifact.json"],
                                "parent_interface_artifacts": ["scaffold"],
                                "expected_output_paths": ["skill.py"],
                                "file_write_claims": ["skill.py"],
                        "acceptance_criteria": ["empty input returns JSON", "tests pass"],
                                "test_expectations": ["empty input returns JSON"],
                                "interface_artifact_expectations": ["declare updated JSON behavior"],
                    },
                        ],
                        "edges": [{"from": "scaffold", "to": "edge_cases", "reason": "edge cases need entrypoint"}],
                        "final_e2e_expectations": ["package satisfies blueprint"],
                    }
                }
                return subprocess.CompletedProcess(
                    args=result.args,
                    returncode=0,
                    stdout=json.dumps(payload),
                    stderr="",
                )
            return result

    adapter = TwoTaskAdapter()
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
    assert [step.milestone_name for step in agent_run.steps if step.step_name == "tester"][:2] == ["scaffold", "edge_cases"]
    scaffold_file = tmp_path / "runtime" / "agent_runs" / f"run_{agent_run.id}" / "tasks" / "scaffold.json"
    edge_file = tmp_path / "runtime" / "agent_runs" / f"run_{agent_run.id}" / "tasks" / "edge_cases.json"
    assert scaffold_file.is_file()
    assert edge_file.is_file()
    scaffold_payload = json.loads(scaffold_file.read_text(encoding="utf-8"))
    assert "blueprint_path" not in scaffold_payload
    assert "permission_path" not in scaffold_payload
    builder_steps = [step for step in agent_run.steps if step.step_name == "builder"]
    assert builder_steps[0].input_json["task_node"]["id"] == "scaffold"
    assert builder_steps[1].input_json["task_node"]["id"] == "edge_cases"
    assert "task_path" not in builder_steps[0].input_json
    assert "task_dag_path" not in builder_steps[0].input_json
    tester_steps = [step for step in agent_run.steps if step.step_name == "tester"]
    assert "task_dag_json" not in tester_steps[0].input_json
    assert "task_path" not in tester_steps[0].input_json


def test_interface_artifact_fallback_distinguishes_created_and_updated_paths(
    tmp_path: Path,
    db_session: Session,
) -> None:
    generation_request = create_generation_request(db_session)

    class ManifestUpdateDagAdapter(FakeCodexAdapter):
        def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
            result = super().generate(prompt, output_dir, plan)
            if plan.get("codex_task") == "product_manager_write_task_dag":
                return subprocess.CompletedProcess(
                    args=result.args,
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "task_dag": {
                                "schema_version": 1,
                                "graph_id": "generated_skill_build",
                                "root_task_ids": ["scaffold"],
                                "nodes": [
                                    {
                                        "id": "scaffold",
                                        "title": "Scaffold",
                                        "summary": "Create executable code against the backend-seeded manifest.",
                                        "depends_on": [],
                                        "difficulty": "easy",
                                        "requires_tests": True,
                                        "parallel_safe": True,
                                        "expected_inputs": ["blueprint.json", "permissions.json"],
                                        "parent_interface_artifacts": [],
                                        "expected_output_paths": ["manifest.json", "skill.py"],
                                        "file_write_claims": ["manifest.json", "skill.py"],
                                        "acceptance_criteria": ["manifest and skill.py exist"],
                                        "test_expectations": ["manifest validates"],
                                        "interface_artifact_expectations": ["declare skill.py entrypoint"],
                                    },
                                    {
                                        "id": "manifest_polish",
                                        "title": "Manifest polish",
                                        "summary": "Update declarative manifest details after code exists.",
                                        "depends_on": ["scaffold"],
                                        "difficulty": "easy",
                                        "requires_tests": True,
                                        "parallel_safe": True,
                                        "expected_inputs": ["manifest.json"],
                                        "parent_interface_artifacts": ["scaffold"],
                                        "expected_output_paths": ["manifest.json"],
                                        "file_write_claims": ["manifest.json"],
                                        "acceptance_criteria": ["manifest remains valid"],
                                        "test_expectations": ["manifest validates"],
                                        "interface_artifact_expectations": ["manifest contract remains stable"],
                                    },
                                ],
                                "edges": [{"from": "scaffold", "to": "manifest_polish"}],
                                "final_e2e_expectations": ["package satisfies blueprint"],
                            }
                        }
                    ),
                    stderr="",
                )
            return result

    service = AgentWorkflowService(
        db_session,
        codex_service=CodexService(db_session, adapter=ManifestUpdateDagAdapter(), project_root=tmp_path),
        project_root=tmp_path,
    )
    agent_run = service.create_build_run(generation_request)
    approve_generation(db_session, generation_request, tmp_path)

    agent_run, _skill, validation = service.continue_build_after_approval(generation_request)

    assert validation.ok is True
    artifact_dir = tmp_path / "runtime" / "agent_runs" / f"run_{agent_run.id}" / "tasks"
    scaffold_artifact = json.loads((artifact_dir / "scaffold" / "interface_artifact.json").read_text(encoding="utf-8"))
    polish_artifact = json.loads(
        (artifact_dir / "manifest_polish" / "interface_artifact.json").read_text(encoding="utf-8")
    )
    assert scaffold_artifact["created_paths"] == ["skill.py"]
    assert scaffold_artifact["updated_paths"] == ["manifest.json"]
    assert polish_artifact["created_paths"] == []
    assert polish_artifact["updated_paths"] == ["manifest.json"]


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
    assert len(tester_plans) == 2
    tester_plan = tester_plans[0]
    assert tester_plan["blueprint_json"]["skill_name"] == skill.name
    assert "skill.py" in tester_plan["code_files"]
    assert any("You are TesterAgent" in prompt for prompt in adapter.prompts)
    assert tester_plan["task_id"] == "core_skill"
    assert tester_plans[-1]["task_id"] == "final_e2e"
    assert any("tests/test_<task_id>.py" in prompt for prompt in adapter.prompts)
    builder_plans = [plan for plan in adapter.plans if plan.get("current_milestone")]
    assert builder_plans[0]["builder_writes_tests"] is False
    assert "task_path" not in builder_plans[0]["current_milestone"]
    assert "task_dag_path" not in builder_plans[0]["current_milestone"]
    assert "task_dag_json" not in tester_plans[0]
    assert "final_e2e_expectations" in tester_plans[-1]
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

    assert adapter.calls == 5
    assert validation.ok is True
    assert agent_run.failure_count_json["core_skill"] == 1
    builder_steps = [step for step in agent_run.steps if step.step_name == "builder"]
    assert builder_steps[0].input_json["mode"] == "build_task"
    assert builder_steps[1].input_json["mode"] == "fix_task"
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
    assert agent_run.status == "blocked"
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


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda dag: dag.update({"root_task_ids": []}), "root_task_ids"),
        (lambda dag: dag["nodes"][0].update({"depends_on": ["second"]}), "cyclic"),
        (lambda dag: dag["nodes"][0].update({"depends_on": ["missing"]}), "missing node"),
        (lambda dag: dag["nodes"][0].update({"id": "../bad"}), "safe path segment"),
        (lambda dag: dag["nodes"][0].update({"acceptance_criteria": []}), "acceptance criteria"),
        (lambda dag: dag["nodes"][0].update({"expected_output_paths": []}), "expected output paths"),
        (lambda dag: dag["nodes"][0].update({"requires_tests": False}), "tested node"),
        (
            lambda dag: dag["nodes"].append(
                {
                    "id": "other",
                    "title": "Other",
                    "summary": "Other work.",
                    "depends_on": [],
                    "difficulty": "easy",
                    "requires_tests": True,
                    "parallel_safe": True,
                    "expected_inputs": [],
                    "parent_interface_artifacts": [],
                    "expected_output_paths": ["skill.py"],
                    "file_write_claims": ["skill.py"],
                    "acceptance_criteria": ["valid"],
                    "test_expectations": ["valid"],
                    "interface_artifact_expectations": [],
                }
            ),
            "overlapping file write claims",
        ),
    ],
)
def test_task_dag_validation_rejects_invalid_graphs(
    db_session: Session,
    mutation,
    message: str,
) -> None:
    dag = {
        "schema_version": 1,
        "graph_id": "generated_skill_build",
        "root_task_ids": ["core_skill"],
        "nodes": [
            {
                "id": "core_skill",
                "title": "Core",
                "summary": "Core work.",
                "depends_on": [],
                "difficulty": "easy",
                "requires_tests": True,
                "parallel_safe": True,
                "expected_inputs": [],
                "parent_interface_artifacts": [],
                "expected_output_paths": ["skill.py"],
                "file_write_claims": ["skill.py"],
                "acceptance_criteria": ["valid"],
                "test_expectations": ["valid"],
                "interface_artifact_expectations": [],
            }
        ],
        "edges": [],
        "final_e2e_expectations": ["valid"],
    }
    if message == "cyclic":
        dag["nodes"].append(
            {
                **copy.deepcopy(dag["nodes"][0]),
                "id": "second",
                "depends_on": ["core_skill"],
            }
        )
    mutation(dag)

    with pytest.raises(AgentWorkflowError, match=message):
        AgentWorkflowService(db_session)._validate_task_dag(dag, {"skill_type": "automation"})


def test_task_dag_sanitizer_removes_tester_owned_paths(db_session: Session) -> None:
    fallback = {
        "schema_version": 1,
        "graph_id": "fallback_build",
        "root_task_ids": ["core_skill"],
        "nodes": [
            {
                "id": "core_skill",
                "title": "Core",
                "summary": "Core work.",
                "depends_on": [],
                "difficulty": "medium",
                "requires_tests": True,
                "parallel_safe": True,
                "expected_inputs": [],
                "parent_interface_artifacts": [],
                "expected_output_paths": ["manifest.json"],
                "file_write_claims": ["manifest.json"],
                "acceptance_criteria": ["valid"],
                "test_expectations": ["valid"],
                "interface_artifact_expectations": [],
            }
        ],
        "edges": [],
        "final_e2e_expectations": ["valid"],
    }
    raw = copy.deepcopy(fallback)
    raw["nodes"] = [
        {
            **copy.deepcopy(fallback["nodes"][0]),
            "id": "manifest_contract",
            "expected_output_paths": ["manifest.json", "tests/test_skill.py"],
            "file_write_claims": ["manifest.json", "tests/test_skill.py"],
        },
        {
            **copy.deepcopy(fallback["nodes"][0]),
            "id": "rules",
            "expected_output_paths": ["skill.py", "tests/test_skill.py"],
            "file_write_claims": ["skill.py", "tests/test_skill.py"],
        },
    ]
    raw["root_task_ids"] = ["manifest_contract"]

    sanitized = CodexService(db_session)._sanitize_task_dag(raw, fallback, {"skill_name": "weather_tool"})

    assert sanitized["nodes"][0]["expected_output_paths"] == ["manifest.json"]
    assert sanitized["nodes"][0]["file_write_claims"] == ["manifest.json"]
    assert sanitized["nodes"][1]["expected_output_paths"] == ["skill.py"]
    assert sanitized["nodes"][1]["file_write_claims"] == ["skill.py"]
    AgentWorkflowService(db_session)._validate_task_dag(sanitized, {"skill_type": "automation"})


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
