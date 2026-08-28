import copy
import json
import subprocess
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import app.services.agent_workflow_service as workflow_module
from app.db import Base
from app.models import (
    AgentRun,
    AgentRunStep,
    ApprovalRequest,
    CodexRoutingSettings,
    Skill,
    SkillGenerationRequest,
    SkillRun,
)
from app.routers.agent_runs import delete_agent_run
from app.services.agent_workflow_service import AgentWorkflowError, AgentWorkflowService
from app.services.chat_orchestrator import ChatOrchestrator
from app.services.codex_service import CodexService
from app.services.default_permissions import blocked_permissions, planning_permission_policy
from app.services.permission_service import PermissionService
from app.services.product_manager_contract_service import ProductManagerContractError
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


def test_product_manager_creates_blueprint_and_permissions_before_task_dag(tmp_path: Path, db_session: Session) -> None:
    generation_request = create_generation_request(db_session)
    agent_run = AgentWorkflowService(db_session, project_root=tmp_path).create_build_run(generation_request)

    building_skill = db_session.get(Skill, generation_request.proposed_skill_id)
    run_dir = tmp_path / "runtime" / "agent_runs" / f"run_{agent_run.id}"

    assert agent_run.status == "waiting_for_approval"
    assert agent_run.current_step == "backend"
    assert agent_run.current_task_id is None
    assert building_skill.status == "building"
    assert agent_run.build_workflow == "task_dag"
    assert "milestones" not in agent_run.blueprint_json
    assert "build_workflow" not in agent_run.blueprint_json
    steps = sorted(agent_run.steps, key=lambda step: step.id)
    assert [step.step_name for step in steps] == ["product_manager"] * 2 + ["backend"]
    assert [step.action for step in steps] == [
        "pm_refine_intent",
        "product_manager_plan_build",
        "backend_build_time_permission_review",
    ]
    assert steps[0].output_json["intent_prompt_path"].endswith("intent_prompt.json")
    assert steps[1].output_json["decision_json"]["decision"] == "proceed_to_approval"
    assert steps[1].output_json["blueprint_json"]["name"] == building_skill.name
    assert steps[1].output_json["permission_path"].endswith("permissions.json")
    assert steps[2].input_json is None
    assert steps[2].output_json is None
    assert steps[2].approval_request_id is not None
    assert steps[0].agent_input_text is None
    assert steps[0].agent_output_text is None
    assert steps[0].codex_invocations_json == []
    assert steps[1].agent_input_text
    assert steps[1].agent_output_text is not None
    assert steps[2].agent_input_text is None
    assert steps[2].agent_output_text is None
    assert (run_dir / "intent_prompt.json").is_file()
    assert (run_dir / "decision.json").is_file()
    assert (run_dir / "blueprint.json").is_file()
    assert (run_dir / "permissions.json").is_file()
    permission_plan = json.loads((run_dir / "permissions.json").read_text(encoding="utf-8"))
    decision_json = json.loads((run_dir / "decision.json").read_text(encoding="utf-8"))
    assert set(decision_json) == {"decision", "user_prompt"}
    assert "permissions" not in permission_plan["runtime"]
    assert "network_domains" not in permission_plan["runtime"]
    assert "codex_generation" not in permission_plan["build_time"]
    assert "reason" not in permission_plan["build_time"]
    assert "reason" not in permission_plan["runtime"]
    assert permission_plan["runtime"]["codex"]["call_response"] is False
    assert isinstance(permission_plan["runtime"]["network"], list)
    assert not (run_dir / "task_dag.json").exists()
    assert not (run_dir / "tasks").exists()
    assert "expected_files" not in agent_run.blueprint_json
    stored_blueprint = json.loads((run_dir / "blueprint.json").read_text(encoding="utf-8"))
    assert "expected_files" not in stored_blueprint
    assert "build_workflow" not in stored_blueprint
    assert set(steps[1].input_json) == {"action", "intent_prompt", "user_reply", "permission_policy"}
    assert steps[1].input_json["permission_policy"] == planning_permission_policy()


def test_settings_workflow_override_wins_over_product_manager_choice(tmp_path: Path, db_session: Session) -> None:
    db_session.add(
        CodexRoutingSettings(
            id=1,
            settings_json={"project_build_workflow_override": "single_codex"},
        )
    )
    db_session.commit()
    generation_request = create_generation_request(db_session)

    agent_run = AgentWorkflowService(db_session, project_root=tmp_path).create_build_run(generation_request)

    blueprint_step = sorted(agent_run.steps, key=lambda step: step.id)[1]
    assert agent_run.build_workflow == "single_codex"
    assert blueprint_step.output_json["product_manager_build_workflow"] == "task_dag"
    assert blueprint_step.output_json["build_workflow"] == "single_codex"
    assert blueprint_step.output_json["build_workflow_source"] == "settings_override"


def test_single_turn_project_request_uses_passthrough_intent_without_codex(
    tmp_path: Path,
    db_session: Session,
) -> None:
    generation_request = create_generation_request(db_session)

    class RecordingAdapter(DeterministicCodexStub):
        def __init__(self) -> None:
            self.tasks: list[str] = []

        def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
            self.tasks.append(str(plan.get("codex_task", "")))
            return super().generate(prompt, output_dir, plan)

    adapter = RecordingAdapter()
    agent_run = AgentWorkflowService(
        db_session,
        codex_service=CodexService(db_session, adapter=adapter, project_root=tmp_path),
        project_root=tmp_path,
    ).create_build_run(generation_request)

    assert "product_manager_refine_intent" not in adapter.tasks
    intent_prompt = json.loads(
        (tmp_path / "runtime" / "agent_runs" / f"run_{agent_run.id}" / "intent_prompt.json").read_text(
            encoding="utf-8"
        )
    )
    assert intent_prompt == {
        "schema_version": 1,
        "refined_prompt": generation_request.user_message,
    }


def test_new_agent_run_archives_stale_artifact_directory(tmp_path: Path, db_session: Session) -> None:
    stale_dir = tmp_path / "runtime" / "agent_runs" / "run_1"
    stale_dir.mkdir(parents=True)
    (stale_dir / "task_dag.json").write_text('{"stale": true}', encoding="utf-8")
    generation_request = create_generation_request(db_session)
    service = AgentWorkflowService(
        db_session,
        codex_service=CodexService(db_session, adapter=DeterministicCodexStub(), project_root=tmp_path),
        project_root=tmp_path,
    )

    agent_run = service.create_build_run(generation_request)

    assert agent_run.id == 1
    assert not (stale_dir / "task_dag.json").exists()
    assert (stale_dir / "blueprint.json").is_file()
    archived = list((tmp_path / "runtime" / "agent_runs" / "orphaned").glob("run_1_*"))
    assert len(archived) == 1
    assert (archived[0] / "task_dag.json").read_text(encoding="utf-8") == '{"stale": true}'


def test_product_manager_uses_codex_adapter_for_blueprint_and_permissions(tmp_path: Path, db_session: Session) -> None:
    generation_request = create_generation_request(db_session)

    class RecordingAdapter(DeterministicCodexStub):
        def __init__(self) -> None:
            self.tasks: list[str] = []
            self.plans: list[dict] = []
            self.prompts: dict[str, str] = {}

        def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
            task = plan.get("codex_task")
            if task:
                self.tasks.append(task)
                self.prompts[task] = prompt
            self.plans.append(dict(plan))
            return super().generate(prompt, output_dir, plan)

    adapter = RecordingAdapter()
    AgentWorkflowService(
        db_session,
        codex_service=CodexService(db_session, adapter=adapter, project_root=tmp_path),
        project_root=tmp_path,
    ).create_build_run(generation_request)

    assert "product_manager_refine_intent" not in adapter.tasks
    assert "product_manager_plan_build" in adapter.tasks
    assert "product_manager_write_permissions" not in adapter.tasks
    assert "product_manager_summary" not in adapter.tasks
    review_plan = next(plan for plan in adapter.plans if plan.get("codex_task") == "product_manager_plan_build")
    assert review_plan["intent_prompt"]["refined_prompt"] == generation_request.user_message
    assert review_plan["permission_policy"] == planning_permission_policy()
    blueprint_prompt = adapter.prompts["product_manager_plan_build"]
    assert '"intent_prompt"' in blueprint_prompt
    assert '"generation_plan"' not in blueprint_prompt
    assert '"user_message"' not in blueprint_prompt
    assert '"permission_policy"' in blueprint_prompt
    assert '"default_allowed"' in blueprint_prompt
    assert '"requires_approval"' in blueprint_prompt
    assert '"blocked"' in blueprint_prompt


def test_approval_updates_waiting_product_manager_permission_step(db_session: Session) -> None:
    response = ChatOrchestrator(db_session).handle_message("Create a reusable local workflow skill.", mode="project")
    permission_request = response["permission_request"]

    PermissionService(db_session).approve_request(permission_request)

    agent_run = AgentWorkflowService(db_session).latest_run_for_generation(response["generation_request"].id)
    db_session.refresh(response["generation_request"])
    backend_step = [step for step in agent_run.steps if step.approval_request_id == permission_request.id][0]
    assert backend_step.step_name == "backend"
    assert backend_step.status == "succeeded"
    assert backend_step.input_json is None
    assert backend_step.output_json is None
    assert agent_run.status == "pending"
    assert response["generation_request"].status == "approved"


def test_resume_after_generic_approval_runs_builder(tmp_path: Path, db_session: Session) -> None:
    generation_request = create_generation_request(db_session)
    agent_run = AgentWorkflowService(db_session, project_root=tmp_path).create_build_run(generation_request)
    permission_request = PermissionService(db_session, project_root=tmp_path).create_build_time_request(generation_request)
    PermissionService(db_session, project_root=tmp_path).approve_request(permission_request)

    resumed = AgentWorkflowService(
        db_session,
        codex_service=CodexService(db_session, adapter=DeterministicCodexStub(), project_root=tmp_path),
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
    adapter = DeterministicCodexStub()

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
        "backend",
    }
    approval_backend_step = [step for step in agent_run.steps if step.approval_request_id is not None][0]
    assert approval_backend_step.status == "succeeded"
    assert "Approved by local user" in approval_backend_step.logs
    runtime_artifact = tmp_path / "runtime" / "agent_runs" / f"run_{agent_run.id}" / "runtime_permissions.json"
    assert runtime_artifact.is_file()
    assert json.loads(runtime_artifact.read_text(encoding="utf-8"))["skill_id"] == skill.id
    final_permission_plan = json.loads(
        (tmp_path / "runtime" / "agent_runs" / f"run_{agent_run.id}" / "permissions.json").read_text(
            encoding="utf-8"
        )
    )
    assert final_permission_plan["runtime"]["filesystem_read"] == ["./cache"]
    assert final_permission_plan["runtime"]["filesystem_write"] == ["./cache"]
    assert final_permission_plan["runtime"]["python_standard_library"] is True
    assert final_permission_plan["runtime"]["codex"]["call_response"] is False
    assert final_permission_plan["build_time"]["dependencies"] == ["pytest", "requests"]
    assert final_permission_plan["build_time"]["project_read"] == ["Eidolon"]
    assert "default_allowed" not in final_permission_plan
    assert "blocked" not in final_permission_plan
    assert "codex_generation" not in final_permission_plan["build_time"]
    assert "reason" not in final_permission_plan["build_time"]
    assert "reason" not in final_permission_plan["runtime"]
    tester_step = [step for step in agent_run.steps if step.step_name == "tester"][0]
    assert "skill.py" in tester_step.input_json["workspace_paths"]
    assert "code_files" not in tester_step.input_json
    assert "tests/test_core_skill.py" in tester_step.output_json["tests_written"]
    assert tester_step.output_json["tester_generation"]["stdout"] == "fake tester wrote tests"
    task_dag_artifact = tmp_path / "runtime" / "agent_runs" / f"run_{agent_run.id}" / "task_dag.json"
    task_artifact = tmp_path / "runtime" / "agent_runs" / f"run_{agent_run.id}" / "tasks" / "core_skill.json"
    interface_artifact = tmp_path / "runtime" / "agent_runs" / f"run_{agent_run.id}" / "tasks" / "core_skill" / "interface_artifact.json"
    assert task_dag_artifact.is_file()
    assert (tmp_path / "runtime" / "function_catalog.json").is_file()
    assert not (tmp_path / "runtime" / "agent_runs" / f"run_{agent_run.id}" / "function_catalog.json").exists()
    dag_step = next(
        step
        for step in agent_run.steps
        if step.step_name == "product_manager" and step.input_json.get("action") == "pm_write_task_dag"
    )
    dependency_step = next(
        step
        for step in agent_run.steps
        if step.step_name == "backend" and step.action == "backend_build_dependency_provisioning"
    )
    builder_step = next(step for step in agent_run.steps if step.step_name == "builder")
    assert dependency_step.status == "succeeded"
    assert dependency_step.input_json is None
    assert dependency_step.output_json is None
    assert dependency_step.id < dag_step.id < builder_step.id
    assert dag_step.input_json["function_catalog_index"] == []
    assert task_artifact.is_file()
    assert interface_artifact.is_file()
    assert "task_id" not in json.loads(interface_artifact.read_text(encoding="utf-8"))
    test_file = tmp_path / "skills" / "proposed" / skill.name / "tests" / "test_core_skill.py"
    test_source = test_file.read_text(encoding="utf-8")
    assert "test_manifest_matches_blueprint_and_safe_contract" in test_source
    assert "test_skill_accepts_representative_input_and_outputs_json_object" in test_source


def test_single_codex_workflow_builds_and_validates_web_app_protocol(
    tmp_path: Path,
    db_session: Session,
) -> None:
    generation_request = create_generation_request(db_session)
    generation_request.plan_json = {
        **generation_request.plan_json,
        "runtime": "web_app",
        "files_to_generate": ["manifest.json", "README.md", "app.py", "tests/test_app.py"],
        "schedule": None,
    }
    db_session.commit()

    class WebAppWorkflowAdapter(DeterministicCodexStub):
        def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
            result = super().generate(prompt, output_dir, plan)
            if plan.get("codex_task") == "product_manager_plan_build":
                payload = json.loads(result.stdout)
                payload["build_workflow"] = "single_codex"
                payload["blueprint"]["runtime"] = "web_app"
                payload["blueprint"]["input_schema"] = None
                payload["blueprint"]["output_schema"] = None
                payload["blueprint"]["schedule"] = None
                return subprocess.CompletedProcess(args=result.args, returncode=0, stdout=json.dumps(payload), stderr="")
            return result

    service = AgentWorkflowService(
        db_session,
        codex_service=CodexService(db_session, adapter=WebAppWorkflowAdapter(), project_root=tmp_path),
        project_root=tmp_path,
    )
    service.create_build_run(generation_request)
    approve_generation(db_session, generation_request, tmp_path)

    agent_run, skill, validation = service.continue_build_after_approval(generation_request)

    skill_dir = tmp_path / "skills" / "proposed" / skill.name
    manifest = json.loads((skill_dir / "manifest.json").read_text(encoding="utf-8"))
    assert validation.ok is True
    assert agent_run.status == "succeeded"
    assert skill.runtime == "web_app"
    assert manifest["runtime"] == "web_app"
    assert manifest["entrypoint"] == "app:app"
    assert not {"risk_level", "created_by", "enabled"} & set(manifest)
    assert (skill_dir / "app.py").is_file()
    assert (skill_dir / "tests" / "test_app.py").is_file()


def test_single_codex_static_scan_blocks_runtime_review_without_calling_more_agents(
    tmp_path: Path,
    db_session: Session,
) -> None:
    generation_request = create_generation_request(db_session)

    class UnsafeSingleWorkflowAdapter(DeterministicCodexStub):
        def __init__(self) -> None:
            self.tasks: list[str] = []

        def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
            task = str(plan.get("codex_task") or "")
            self.tasks.append(task)
            result = super().generate(prompt, output_dir, plan)
            if task == "product_manager_plan_build":
                payload = json.loads(result.stdout)
                payload["build_workflow"] = "single_codex"
                return subprocess.CompletedProcess(args=result.args, returncode=0, stdout=json.dumps(payload), stderr="")
            if task == "single_codex_build":
                skill_path = output_dir / "skill.py"
                skill_path.write_text(
                    "import subprocess\nsubprocess.run(['blocked'])\n" + skill_path.read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
            return result

    adapter = UnsafeSingleWorkflowAdapter()
    service = AgentWorkflowService(
        db_session,
        codex_service=CodexService(db_session, adapter=adapter, project_root=tmp_path),
        project_root=tmp_path,
    )
    agent_run = service.create_build_run(generation_request)
    approve_generation(db_session, generation_request, tmp_path)

    agent_run, _skill, validation = service.continue_build_after_approval(generation_request)

    run_dir = tmp_path / "runtime" / "agent_runs" / f"run_{agent_run.id}"
    scan = json.loads((run_dir / "capability_scan.json").read_text(encoding="utf-8"))
    assert validation.ok is False
    assert agent_run.status == "blocked"
    assert scan["ok"] is False
    assert any(finding["capability"] == "shell_process" for finding in scan["findings"])
    assert adapter.tasks.count("single_codex_build") == 1
    assert "tester_write_tests" not in adapter.tasks
    assert "skill_repair" not in adapter.tasks
    assert not (run_dir / "runtime_permissions.json").exists()

    with pytest.raises(AgentWorkflowError, match="cannot be retried after an error"):
        service.retry_current_task(agent_run)

    db_session.refresh(agent_run)
    assert agent_run.status == "blocked"
    assert adapter.tasks.count("single_codex_build") == 1


def test_failed_single_codex_resume_does_not_restart_builder(tmp_path: Path, db_session: Session) -> None:
    agent_run = AgentRun(
        run_type="build_skill",
        status="failed",
        user_request="Build a small app",
        build_workflow="single_codex",
        current_task_id="single_codex",
    )
    db_session.add(agent_run)
    db_session.commit()
    service = AgentWorkflowService(db_session, project_root=tmp_path)

    with pytest.raises(AgentWorkflowError, match="cannot be retried after an error"):
        service.resume_run(agent_run)

    db_session.refresh(agent_run)
    assert agent_run.status == "failed"


def test_final_capability_scan_uses_actual_manifest_permissions(
    tmp_path: Path,
    db_session: Session,
) -> None:
    generation_request = create_generation_request(db_session)

    class ManifestMismatchAdapter(DeterministicCodexStub):
        def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
            task = str(plan.get("codex_task") or "")
            result = super().generate(prompt, output_dir, plan)
            if task == "product_manager_plan_build":
                payload = json.loads(result.stdout)
                payload["build_workflow"] = "single_codex"
                payload["permission_plan"]["runtime"]["network"] = ["example.com"]
                return subprocess.CompletedProcess(args=result.args, returncode=0, stdout=json.dumps(payload), stderr="")
            if task == "single_codex_build":
                manifest_path = output_dir / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["permissions"]["network"] = []
                manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
                (output_dir / "skill.py").write_text(
                    "import requests\nrequests.get('https://example.com/data')\n",
                    encoding="utf-8",
                )
            return result

    service = AgentWorkflowService(
        db_session,
        codex_service=CodexService(db_session, adapter=ManifestMismatchAdapter(), project_root=tmp_path),
        project_root=tmp_path,
    )
    agent_run = service.create_build_run(generation_request)
    approve_generation(db_session, generation_request, tmp_path)

    agent_run, _skill, validation = service.continue_build_after_approval(generation_request)

    scan_path = tmp_path / "runtime" / "agent_runs" / f"run_{agent_run.id}" / "capability_scan.json"
    scan = json.loads(scan_path.read_text(encoding="utf-8"))
    assert validation.ok is False
    assert agent_run.status == "blocked"
    assert any(
        finding["capability"] == "network" and finding["status"] == "undeclared"
        for finding in scan["findings"]
    )


def test_backend_seeds_manifest_schedule_from_product_manager_blueprint(
    tmp_path: Path,
    db_session: Session,
) -> None:
    generation_request = create_generation_request(db_session)
    generation_request.plan_json = {
        **generation_request.plan_json,
        "schedule": {
            "type": "weekly",
            "day": "monday",
            "time": "09:00",
            "timezone": "America/Toronto",
            "input": {},
        },
    }
    db_session.commit()
    AgentWorkflowService(db_session, project_root=tmp_path).create_build_run(generation_request)
    approve_generation(db_session, generation_request, tmp_path)

    _agent_run, skill, validation = AgentWorkflowService(
        db_session,
        codex_service=CodexService(db_session, adapter=DeterministicCodexStub(), project_root=tmp_path),
        project_root=tmp_path,
    ).continue_build_after_approval(generation_request)

    assert validation.ok is True
    manifest = json.loads((tmp_path / "skills" / "proposed" / skill.name / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schedule"] == generation_request.plan_json["schedule"]


def test_build_workflow_executes_pm_task_dag_in_dependency_order(tmp_path: Path, db_session: Session) -> None:
    generation_request = create_generation_request(db_session)

    class TwoTaskAdapter(DeterministicCodexStub):
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
                        "task_prompt": "Create the basic skill package files.",
                                "depends_on": [],
                                "difficulty": "easy",
                                "requires_tests": True,
                                "parallel_safe": True,
                                "expected_inputs": ["blueprint.json", "permissions.json"],
                                "parent_interface_artifacts": [],
                                "write_paths": ["README.md", "skill.py"],
                        "acceptance_criteria": ["manifest.json is valid", "skill.py exists"],
                                "test_expectations": ["manifest validates"],
                                "function_ids": [],
                    },
                    {
                        "id": "edge_cases",
                        "task_prompt": "Add input edge case behavior.",
                                "depends_on": ["scaffold"],
                                "difficulty": "medium",
                                "requires_tests": True,
                                "parallel_safe": True,
                                "expected_inputs": ["tasks/scaffold/interface_artifact.json"],
                                "parent_interface_artifacts": ["scaffold"],
                                "write_paths": ["skill.py"],
                        "acceptance_criteria": ["empty input returns JSON", "tests pass"],
                                "test_expectations": ["empty input returns JSON"],
                                "function_ids": [],
                    },
                        ],
                        "edges": [{"from": "scaffold", "to": "edge_cases", "reason": "edge cases need entrypoint"}],
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
    assert agent_run.final_summary_json["task_statuses"] == {"scaffold": "done", "edge_cases": "done"}
    assert [step.task_node_id for step in agent_run.steps if step.step_name == "builder"] == ["scaffold", "edge_cases"]
    assert [step.task_node_id for step in agent_run.steps if step.step_name == "tester"][:2] == ["scaffold", "edge_cases"]
    scaffold_file = tmp_path / "runtime" / "agent_runs" / f"run_{agent_run.id}" / "tasks" / "scaffold.json"
    edge_file = tmp_path / "runtime" / "agent_runs" / f"run_{agent_run.id}" / "tasks" / "edge_cases.json"
    assert scaffold_file.is_file()
    assert edge_file.is_file()
    scaffold_payload = json.loads(scaffold_file.read_text(encoding="utf-8"))
    assert "blueprint_path" not in scaffold_payload
    assert "permission_path" not in scaffold_payload
    builder_steps = [step for step in agent_run.steps if step.step_name == "builder"]
    assert builder_steps[0].input_json["task_node"] == {
        "task_prompt": "Create the basic skill package files.",
        "write_paths": ["README.md", "skill.py"],
        "acceptance_criteria": ["manifest.json is valid", "skill.py exists"],
    }
    assert builder_steps[1].input_json["task_node"] == {
        "task_prompt": "Add input edge case behavior.",
        "write_paths": ["skill.py"],
        "acceptance_criteria": ["empty input returns JSON", "tests pass"],
    }
    assert "skill.py" in builder_steps[0].input_json["workspace_paths"]
    assert "skill.py" in builder_steps[1].input_json["workspace_paths"]
    assert all("code_files" not in step.input_json for step in builder_steps)
    assert "task_path" not in builder_steps[0].input_json
    assert "task_dag_path" not in builder_steps[0].input_json
    tester_steps = [step for step in agent_run.steps if step.step_name == "tester"]
    assert "task_dag_json" not in tester_steps[0].input_json
    assert "task_path" not in tester_steps[0].input_json


def test_builder_receives_function_context_for_task_node(tmp_path: Path, db_session: Session) -> None:
    generation_request = create_generation_request(db_session)

    class ApiTaskAdapter(DeterministicCodexStub):
        def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
            result = super().generate(prompt, output_dir, plan)
            if plan.get("codex_task") == "product_manager_plan_build":
                payload = json.loads(result.stdout)
                payload["blueprint"]["functions"] = ["backend.codex.call"]
                return subprocess.CompletedProcess(args=result.args, returncode=0, stdout=json.dumps(payload), stderr="")
            if plan.get("codex_task") == "product_manager_write_task_dag":
                payload = json.loads(result.stdout)
                payload["task_dag"]["nodes"][0]["function_ids"] = ["backend.codex.call"]
                return subprocess.CompletedProcess(args=result.args, returncode=0, stdout=json.dumps(payload), stderr="")
            return result

    service = AgentWorkflowService(
        db_session,
        codex_service=CodexService(db_session, adapter=ApiTaskAdapter(), project_root=tmp_path),
        project_root=tmp_path,
    )
    agent_run = service.create_build_run(generation_request)
    approve_generation(db_session, generation_request, tmp_path)

    agent_run, _skill, validation = service.continue_build_after_approval(generation_request)

    assert validation.ok is True
    builder_step = next(step for step in agent_run.steps if step.step_name == "builder")
    assert "function_ids" not in builder_step.input_json["task_node"]
    function_context = builder_step.input_json["function_context"]
    assert function_context[0]["title"] == "Skill Codex Call"
    assert "function_runtime_capabilities.call_codex" in function_context[0]["invocation"]["function_helper"]
    assert "input_schema" in function_context[0]
    assert "output_schema" in function_context[0]


def test_builder_interface_artifact_is_validated_and_moved_to_agent_run(
    tmp_path: Path,
    db_session: Session,
) -> None:
    generation_request = create_generation_request(db_session)

    class ManifestUpdateDagAdapter(DeterministicCodexStub):
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
                                        "task_prompt": "Create executable code against the backend-seeded manifest.",
                                        "depends_on": [],
                                        "difficulty": "easy",
                                        "requires_tests": True,
                                        "parallel_safe": True,
                                        "expected_inputs": ["blueprint.json", "permissions.json"],
                                        "parent_interface_artifacts": [],
                                        "write_paths": ["skill.py"],
                                        "acceptance_criteria": ["manifest and skill.py exist"],
                                        "test_expectations": ["manifest validates"],
                                        "function_ids": [],
                                    },
                                    {
                                        "id": "skill_polish",
                                        "task_prompt": "Update the implementation after its initial contract exists.",
                                        "depends_on": ["scaffold"],
                                        "difficulty": "easy",
                                        "requires_tests": True,
                                        "parallel_safe": True,
                                        "expected_inputs": ["skill.py"],
                                        "parent_interface_artifacts": ["scaffold"],
                                        "write_paths": ["skill.py"],
                                        "acceptance_criteria": ["skill remains valid"],
                                        "test_expectations": ["manifest validates"],
                                        "function_ids": [],
                                    },
                                ],
                                "edges": [{"from": "scaffold", "to": "skill_polish"}],
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
        (artifact_dir / "skill_polish" / "interface_artifact.json").read_text(encoding="utf-8")
    )
    assert scaffold_artifact["created_paths"] == ["skill.py"]
    assert scaffold_artifact["updated_paths"] == []
    assert polish_artifact["created_paths"] == []
    assert polish_artifact["updated_paths"] == ["skill.py"]
    skill_dir = tmp_path / "skills" / "proposed" / generation_request.plan_json["skill_name"]
    assert not (skill_dir / "interface_artifact.json").exists()


def test_agent_prompts_receive_only_direct_parent_interface_artifacts(
    tmp_path: Path,
    db_session: Session,
) -> None:
    generation_request = create_generation_request(db_session)
    service = AgentWorkflowService(
        db_session,
        codex_service=CodexService(db_session, adapter=DeterministicCodexStub(), project_root=tmp_path),
        project_root=tmp_path,
    )
    agent_run = service.create_build_run(generation_request)
    service.artifacts.write_json(
        agent_run,
        "task_dag.json",
        {
            "nodes": [
                {"id": "root", "depends_on": []},
                {"id": "middle", "depends_on": ["root"]},
                {"id": "leaf", "depends_on": ["middle"]},
            ]
        },
    )
    for task_id in ("root", "middle"):
        path = tmp_path / "runtime" / "agent_runs" / f"run_{agent_run.id}" / "tasks" / task_id
        path.mkdir(parents=True, exist_ok=True)
        (path / "interface_artifact.json").write_text(
            json.dumps({"task_id": task_id, "contracts_for_children": [f"{task_id} contract"]}),
            encoding="utf-8",
        )

    direct = service.artifacts.direct_parent_interface_artifacts(
        agent_run, {"id": "leaf", "depends_on": ["middle"]}
    )
    transitive = service.artifacts.parent_interface_artifacts(
        agent_run, {"id": "leaf", "depends_on": ["middle"]}
    )

    assert [artifact["task_id"] for artifact in direct] == ["middle"]
    assert [artifact["task_id"] for artifact in transitive] == ["root", "middle"]


def test_invalid_builder_interface_artifact_is_not_moved_to_agent_run(
    tmp_path: Path,
    db_session: Session,
) -> None:
    generation_request = create_generation_request(db_session)

    class InvalidArtifactAdapter(DeterministicCodexStub):
        def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
            result = super().generate(prompt, output_dir, plan)
            if isinstance(plan.get("task_context"), dict) and plan.get("codex_task") != "tester_write_tests":
                (output_dir / "interface_artifact.json").write_text(
                    json.dumps({"task_id": "core_skill"}),
                    encoding="utf-8",
                )
            return result

    service = AgentWorkflowService(
        db_session,
        codex_service=CodexService(db_session, adapter=InvalidArtifactAdapter(), project_root=tmp_path),
        project_root=tmp_path,
    )
    agent_run = service.create_build_run(generation_request)
    approve_generation(db_session, generation_request, tmp_path)

    with pytest.raises(AgentWorkflowError, match="interface_artifact.json is invalid"):
        service.continue_build_after_approval(generation_request)

    destination = (
        tmp_path
        / "runtime"
        / "agent_runs"
        / f"run_{agent_run.id}"
        / "tasks"
        / "core_skill"
        / "interface_artifact.json"
    )
    source = tmp_path / "skills" / "proposed" / generation_request.plan_json["skill_name"] / "interface_artifact.json"
    assert not destination.exists()
    assert source.is_file()


def test_builder_and_tester_agents_receive_trimmed_task_context(tmp_path: Path, db_session: Session) -> None:
    generation_request = create_generation_request(db_session)
    AgentWorkflowService(db_session, project_root=tmp_path).create_build_run(generation_request)
    approve_generation(db_session, generation_request, tmp_path)

    class RecordingAdapter(DeterministicCodexStub):
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
    assert "blueprint_json" not in tester_plan
    assert "permission_plan" not in tester_plan
    assert tester_plan["permission_bounds"]["blocked"] == blocked_permissions()
    assert "skill.py" in tester_plan["workspace_paths"]
    assert "code_files" not in tester_plan
    assert any("You are TesterAgent" in prompt for prompt in adapter.prompts)
    assert tester_plan["task_node"] == {
        "task_prompt": "Create the core proposed skill package.",
        "acceptance_criteria": agent_run.blueprint_json["expected_behavior"],
        "test_expectations": ["validate manifest and generated skill behavior"],
    }
    assert tester_plan["test_file"] == "tests/test_core_skill.py"
    assert tester_plans[-1]["test_file"] == "tests/test_final_e2e.py"
    assert tester_plans[-1]["blueprint_contract"]["description"]
    assert "skill_name" not in tester_plans[-1]["blueprint_contract"]
    assert "permission_plan" not in tester_plans[-1]
    assert tester_plans[-1]["permission_bounds"]["blocked"] == blocked_permissions()
    assert "skill.py" in tester_plans[-1]["workspace_paths"]
    assert "codex_last_message.txt" not in tester_plans[-1]["workspace_paths"]
    assert any("tests/test_<task_id>.py" in prompt for prompt in adapter.prompts)
    builder_plans = [plan for plan in adapter.plans if plan.get("task_context")]
    assert builder_plans[0]["builder_writes_tests"] is False
    assert "task_path" not in builder_plans[0]["task_context"]
    assert "task_dag_path" not in builder_plans[0]["task_context"]
    assert "blueprint_json" not in builder_plans[0]["task_context"]
    assert "manifest_requirements" not in builder_plans[0]["task_context"]
    assert "interface_artifact" not in builder_plans[0]["task_context"]
    assert "code_files" not in builder_plans[0]["task_context"]
    permission_bounds = builder_plans[0]["task_context"]["permission_bounds"]
    assert "source" not in permission_bounds
    assert permission_bounds["runtime"]["shell"] is False
    assert permission_bounds["runtime"]["secrets"] == []
    assert permission_bounds["runtime"]["python_standard_library"] is True
    assert permission_bounds["build_time"]["project_read"] == ["Eidolon"]
    assert permission_bounds["blocked"] == blocked_permissions()
    assert "task_dag_json" not in tester_plans[0]
    assert "final_e2e_expectations" not in tester_plans[-1]
    assert set(agent_run.blueprint_json["expected_behavior"]).issubset(
        tester_plans[-1]["acceptance_criteria"]
    )
    task_dag_prompt = next(
        prompt
        for prompt, plan in zip(adapter.prompts, adapter.plans, strict=True)
        if plan.get("codex_task") == "product_manager_write_task_dag"
    )
    assert '"blueprint_json"' in task_dag_prompt
    assert '"permission_bounds"' in task_dag_prompt
    assert '"default_allowed"' not in task_dag_prompt
    assert '"blocked"' in task_dag_prompt
    assert '"function_catalog_index"' in task_dag_prompt
    assert '"intent_prompt"' not in task_dag_prompt
    assert '"generation_plan"' not in task_dag_prompt
    builder_prompt = next(
        prompt
        for prompt, plan in zip(adapter.prompts, adapter.plans, strict=True)
        if isinstance(plan.get("task_context"), dict) and plan.get("codex_task") != "tester_write_tests"
    )
    assert "Builder context:" in builder_prompt
    assert "Generation plan:" not in builder_prompt
    assert agent_run.status == "succeeded"
    run_dir = tmp_path / "runtime" / "agent_runs" / f"run_{agent_run.id}"
    assert json.loads((run_dir / "capability_scan.json").read_text(encoding="utf-8"))["ok"] is True


def test_failed_test_triggers_builder_repair(tmp_path: Path, db_session: Session) -> None:
    generation_request = create_generation_request(db_session)
    AgentWorkflowService(db_session, project_root=tmp_path).create_build_run(generation_request)
    approve_generation(db_session, generation_request, tmp_path)

    class RepairingAdapter(DeterministicCodexStub):
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
    repair_contract = builder_steps[1].input_json["failure_context"]["current_interface_artifact"]
    assert repair_contract["task_id"] == "core_skill"
    assert repair_contract["created_paths"] == ["README.md", "skill.py"]
    assert repair_contract["updated_paths"] == []
    tester_steps = [step for step in agent_run.steps if step.step_name == "tester"]
    assert tester_steps[0].status == "failed"
    assert tester_steps[-1].status == "succeeded"


def test_failed_task_resume_reuses_dag_and_current_interface_contract(
    tmp_path: Path,
    db_session: Session,
) -> None:
    generation_request = create_generation_request(db_session)
    AgentWorkflowService(db_session, project_root=tmp_path).create_build_run(generation_request)
    approve_generation(db_session, generation_request, tmp_path)

    class ResumeAdapter(DeterministicCodexStub):
        def __init__(self) -> None:
            self.non_pm_calls = 0
            self.task_dag_calls = 0

        def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
            if plan.get("codex_task") == "product_manager_write_task_dag":
                self.task_dag_calls += 1
            if not plan.get("codex_task", "").startswith("product_manager"):
                self.non_pm_calls += 1
            result = super().generate(prompt, output_dir, plan)
            if self.non_pm_calls == 1:
                (output_dir / "skill.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
            if self.non_pm_calls == 3:
                artifact = json.loads((output_dir / "interface_artifact.json").read_text(encoding="utf-8"))
                artifact["created_paths"] = []
                (output_dir / "interface_artifact.json").write_text(json.dumps(artifact), encoding="utf-8")
            return result

    adapter = ResumeAdapter()
    service = AgentWorkflowService(
        db_session,
        codex_service=CodexService(db_session, adapter=adapter, project_root=tmp_path),
        project_root=tmp_path,
    )

    with pytest.raises(AgentWorkflowError, match="does not declare required task write paths"):
        service.continue_build_after_approval(generation_request)

    agent_run = service.latest_run_for_generation(generation_request.id)
    resumed = service.retry_current_task(agent_run)

    assert resumed.status == "succeeded"
    assert adapter.task_dag_calls == 1
    builder_modes = [step.input_json["mode"] for step in resumed.steps if step.step_name == "builder"]
    assert builder_modes == ["build_task", "fix_task", "fix_task"]
    retry_context = [step for step in resumed.steps if step.step_name == "builder"][-1].input_json["failure_context"]
    assert "skill.py" in retry_context["current_interface_artifact"]["created_paths"]


def test_more_than_three_failures_stops_workflow(tmp_path: Path, db_session: Session) -> None:
    generation_request = create_generation_request(db_session)
    AgentWorkflowService(db_session, project_root=tmp_path).create_build_run(generation_request)
    approve_generation(db_session, generation_request, tmp_path)

    class AlwaysFailingTestsAdapter(DeterministicCodexStub):
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
    backend_steps = [step for step in agent_run.steps if step.step_name == "backend"]
    assert backend_steps[-1].action == "backend_stop_failed_build"
    assert backend_steps[-1].input_json is None
    assert backend_steps[-1].output_json is None


def test_builder_user_action_required_blocks_workflow(tmp_path: Path, db_session: Session) -> None:
    generation_request = create_generation_request(db_session)
    AgentWorkflowService(db_session, project_root=tmp_path).create_build_run(generation_request)
    approve_generation(db_session, generation_request, tmp_path)

    class UserActionAdapter(DeterministicCodexStub):
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
        (lambda dag: dag["nodes"][0].update({"depends_on": ["second"]}), "cyclic"),
        (lambda dag: dag["nodes"][0].update({"depends_on": ["missing"]}), "missing node"),
        (lambda dag: dag["nodes"][0].update({"id": "../bad"}), "safe path segment"),
        (lambda dag: dag["nodes"][0].update({"task_prompt": ""}), "task prompt"),
        (lambda dag: dag["nodes"][0].update({"acceptance_criteria": []}), "acceptance criteria"),
        (lambda dag: dag["nodes"][0].update({"write_paths": []}), "write paths"),
        (lambda dag: dag["nodes"][0].update({"requires_tests": False}), "tested node"),
        (
            lambda dag: dag["nodes"].append(
                {
                    "id": "other",
                    "task_prompt": "Build other work.",
                    "depends_on": [],
                    "difficulty": "easy",
                    "requires_tests": True,
                    "parallel_safe": True,
                    "expected_inputs": [],
                    "parent_interface_artifacts": [],
                    "write_paths": ["skill.py"],
                    "acceptance_criteria": ["valid"],
                    "test_expectations": ["valid"],
                    "function_ids": [],
                }
            ),
            "overlapping write paths",
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
                "task_prompt": "Build the core work.",
                "depends_on": [],
                "difficulty": "easy",
                "requires_tests": True,
                "parallel_safe": True,
                "expected_inputs": [],
                "parent_interface_artifacts": [],
                "write_paths": ["skill.py"],
                "acceptance_criteria": ["valid"],
                "test_expectations": ["valid"],
                "function_ids": [],
            }
        ],
        "edges": [],
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
        AgentWorkflowService(db_session).task_dags.validate(dag, {})


def test_task_dag_sanitizer_removes_tester_owned_paths(db_session: Session) -> None:
    fallback = {
        "schema_version": 1,
        "graph_id": "fallback_build",
        "root_task_ids": ["core_skill"],
        "nodes": [
            {
                "id": "core_skill",
                "task_prompt": "Build the core work.",
                "depends_on": [],
                "difficulty": "medium",
                "requires_tests": True,
                "parallel_safe": True,
                "expected_inputs": [],
                "parent_interface_artifacts": [],
                "write_paths": ["skill.py"],
                "acceptance_criteria": ["valid"],
                "test_expectations": ["valid"],
                "function_ids": [],
            }
        ],
        "edges": [],
    }
    raw = copy.deepcopy(fallback)
    raw["nodes"] = [
        {
            **copy.deepcopy(fallback["nodes"][0]),
            "id": "rules",
            "write_paths": ["skill.py", "tests/test_skill.py"],
        },
    ]
    raw["root_task_ids"] = ["rules"]

    sanitized = CodexService(db_session).product_manager_contracts.sanitize_task_dag(
        raw, fallback, {"skill_name": "weather_tool"}
    )

    assert "graph_id" not in sanitized
    assert "root_task_ids" not in sanitized
    assert "expected_inputs" not in sanitized["nodes"][0]
    assert sanitized["nodes"][0]["write_paths"] == ["skill.py"]
    AgentWorkflowService(db_session).task_dags.validate(sanitized, {})


def test_task_dag_sanitizer_does_not_read_package_files_from_blueprint(db_session: Session) -> None:
    node = {
        "id": "core_skill",
        "task_prompt": "Build the core skill.",
        "depends_on": [],
        "difficulty": "medium",
        "requires_tests": True,
        "parallel_safe": True,
        "expected_inputs": [],
        "write_paths": ["skill.py"],
        "acceptance_criteria": ["valid"],
        "test_expectations": ["valid"],
        "function_ids": [],
    }
    fallback = {
        "schema_version": 1,
        "graph_id": "fallback_build",
        "root_task_ids": ["core_skill"],
        "nodes": [copy.deepcopy(node)],
    }
    raw = {**fallback, "graph_id": "raw_build", "nodes": [copy.deepcopy(node)]}

    sanitized = CodexService(db_session).product_manager_contracts.sanitize_task_dag(
        raw,
        fallback,
        {
            "skill_name": "github_trending_analysis",
            "expected_files": ["manifest.json", "skill.py", "SKILL.md", "README.md"],
        },
    )

    node = sanitized["nodes"][0]
    assert "graph_id" not in sanitized
    assert "root_task_ids" not in sanitized
    assert "expected_inputs" not in node
    assert node["write_paths"] == ["skill.py"]


def test_blueprint_sanitizer_removes_expected_files(db_session: Session) -> None:
    fallback = {
        "goal": "Fallback goal",
        "skill_name": "fallback_skill",
        "schedule": None,
    }
    raw = {
        **fallback,
        "skill_name": "generated_skill",
        "expected_files": ["manifest.json", "skill.py", "SKILL.md", "README.md"],
        "summary": "discard me",
        "decision": "request_permission",
        "arbitrary": {"field": True},
    }

    sanitized = CodexService(db_session).product_manager_contracts.sanitize_blueprint(raw, fallback)

    assert sanitized["skill_name"] == "generated_skill"
    assert "expected_files" not in sanitized
    assert "summary" not in sanitized
    assert "decision" not in sanitized
    assert "arbitrary" not in sanitized


def test_plan_build_sanitizer_enforces_terminal_contract_fields(db_session: Session) -> None:
    sanitized = CodexService(db_session).product_manager_contracts.sanitize_plan_build(
        {
            "decision": "stop_inplausible",
            "user_prompt": "This requires blocked deletion. Try a non-destructive report instead.",
            "build_workflow": None,
            "blueprint": None,
            "permission_plan": None,
        },
        {},
    )

    assert sanitized == {
        "decision": "stop_inplausible",
        "user_prompt": "This requires blocked deletion. Try a non-destructive report instead.",
        "build_workflow": None,
        "blueprint": None,
        "permission_plan": None,
    }


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (
            {
                "decision": "ask_user_for_input",
                "user_prompt": "Tell me the recurring source",
                "build_workflow": None,
                "blueprint": None,
                "permission_plan": None,
            },
            "exactly one question",
        ),
        (
            {
                "decision": "ask_user_for_input",
                "user_prompt": "Which source should be used?",
                "build_workflow": "single_codex",
                "blueprint": None,
                "permission_plan": None,
            },
            "cannot include planning artifacts",
        ),
        (
            {
                "decision": "stop_inplausible",
                "user_prompt": "Automatic deletion is blocked.",
                "build_workflow": None,
                "blueprint": None,
                "permission_plan": None,
            },
            "safe alternative",
        ),
        (
            {
                "decision": "proceed_to_approval",
                "user_prompt": None,
                "build_workflow": "single_codex",
                "blueprint": None,
                "permission_plan": None,
            },
            "requires blueprint and permissions",
        ),
    ],
)
def test_plan_build_sanitizer_fails_closed_on_invalid_branches(
    db_session: Session,
    response: dict[str, object],
    error: str,
) -> None:
    with pytest.raises(ProductManagerContractError, match=error):
        CodexService(db_session).product_manager_contracts.sanitize_plan_build(response, {})


def test_permission_review_records_permission_expansion(tmp_path: Path, db_session: Session) -> None:
    generation_request = create_generation_request(db_session)
    AgentWorkflowService(db_session, project_root=tmp_path).create_build_run(generation_request)
    approve_generation(db_session, generation_request, tmp_path)

    class ExpandedPermissionAdapter(DeterministicCodexStub):
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
        codex_service=CodexService(db_session, adapter=DeterministicCodexStub(), project_root=tmp_path),
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
    with pytest.raises(Exception, match="cannot be approved"):
        approve_generation(db_session, generation_request, tmp_path)

    with pytest.raises(AgentWorkflowError, match="cancelled"):
        service.continue_build_after_approval(generation_request)

    skill = db_session.get(Skill, agent_run.skill_id)
    assert agent_run.status == "cancelled"
    assert generation_request.status == "cancelled"
    assert skill.status == "failed"
    with pytest.raises(AgentWorkflowError, match="cancelled"):
        service._finalize_validated_skill(agent_run, skill, SimpleNamespace(ok=True))
    assert db_session.query(AgentRunStep).filter_by(agent_run_id=agent_run.id).count() == 3
    assert db_session.query(ApprovalRequest).filter_by(skill_id=skill.id).count() == 0


def test_cancelling_while_waiting_for_clarification_archives_session(
    tmp_path: Path,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ClarifyingAdapter(DeterministicCodexStub):
        def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
            if plan.get("codex_task") == "product_manager_plan_build":
                return subprocess.CompletedProcess(
                    args=["fake"],
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "decision": "ask_user_for_input",
                            "user_prompt": "Which recurring source should the skill process?",
                            "build_workflow": None,
                            "blueprint": None,
                            "permission_plan": None,
                        }
                    ),
                    stderr="",
                )
            return super().generate(prompt, output_dir, plan)

    generation_request = create_generation_request(db_session)
    codex_service = CodexService(
        db_session,
        adapter=ClarifyingAdapter(),
        project_root=tmp_path,
    )
    archived: list[int] = []
    monkeypatch.setattr(
        codex_service,
        "archive_product_manager_thread",
        lambda request: archived.append(request.id),
    )
    service = AgentWorkflowService(
        db_session,
        codex_service=codex_service,
        project_root=tmp_path,
    )
    agent_run = service.create_build_run(generation_request)

    assert agent_run.status == "blocked"
    assert generation_request.status == "needs_input"
    service.cancel_run(agent_run)

    assert agent_run.status == "cancelled"
    assert generation_request.status == "cancelled"
    assert archived == [generation_request.id]


def test_generation_planning_lock_rejects_overlapping_clarification_turns(
    tmp_path: Path,
    db_session: Session,
) -> None:
    generation_request = create_generation_request(db_session)
    lock = workflow_module.threading.Lock()
    lock.acquire()
    with workflow_module._GENERATION_PLANNING_LOCKS_GUARD:
        workflow_module._GENERATION_PLANNING_LOCKS[generation_request.id] = lock
    try:
        with pytest.raises(AgentWorkflowError, match="already processing"):
            AgentWorkflowService(db_session, project_root=tmp_path).create_build_run(generation_request)
    finally:
        lock.release()
        with workflow_module._GENERATION_PLANNING_LOCKS_GUARD:
            workflow_module._GENERATION_PLANNING_LOCKS.pop(generation_request.id, None)


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
        "entrypoint": "skill.py",
        "instructions_path": None,
        "input_schema": None,
        "output_schema": None,
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
