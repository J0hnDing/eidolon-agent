from pathlib import Path
from subprocess import CompletedProcess

from app.models import AgentRun
from app.services.agent_run_artifact_store import AgentRunArtifactStore
from app.services.codex_invocation_recorder import CodexInvocationRecorder
from app.services.product_manager_contract_service import ProductManagerContractService
from app.services.task_dag_service import TaskDagService


def test_task_dag_service_validates_and_batches_ready_nodes() -> None:
    service = TaskDagService()
    task_dag = {
        "nodes": [
            {
                "id": "alpha",
                "task_prompt": "Build alpha.",
                "depends_on": [],
                "parallel_safe": True,
                "requires_tests": True,
                "write_paths": ["alpha.py"],
                "acceptance_criteria": ["alpha passes"],
            },
            {
                "id": "beta",
                "task_prompt": "Build beta.",
                "depends_on": [],
                "parallel_safe": True,
                "requires_tests": False,
                "write_paths": ["beta.py"],
                "acceptance_criteria": ["beta passes"],
            },
            {
                "id": "merge",
                "task_prompt": "Merge alpha and beta.",
                "depends_on": ["alpha", "beta"],
                "parallel_safe": False,
                "requires_tests": True,
                "write_paths": ["skill.py"],
                "acceptance_criteria": ["integration passes"],
            },
        ]
    }

    service.validate(task_dag, {})
    assert [[node["id"] for node in batch] for batch in service.execution_batches(task_dag)] == [
        ["alpha", "beta"],
        ["merge"],
    ]


def test_product_manager_contract_service_keeps_only_backend_fields() -> None:
    service = ProductManagerContractService()
    fallback = {
        "goal": "Fallback",
        "skill_name": "sample",
        "requested_permissions": {"filesystem_write": ["./cache", "report.json"]},
    }

    blueprint = service.sanitize_blueprint(
        {"goal": "Build it", "skill_name": "untrusted_rename", "unknown": "drop"},
        fallback,
    )

    assert "unknown" not in blueprint
    assert blueprint["skill_name"] == "untrusted_rename"
    assert "filesystem_write" not in blueprint["permission_plan"]["runtime"]  # type: ignore[operator]


def test_product_manager_contract_keeps_web_app_approval_requests() -> None:
    service = ProductManagerContractService()
    fallback = {
        "goal": "Web app",
        "runtime": "web_app",
        "skill_name": "web_app",
        "requested_permissions": {
            "network": ["example.com"],
            "codex": {"internet_access": True},
        },
    }

    blueprint = service.sanitize_blueprint({"runtime": "web_app"}, fallback)
    runtime = blueprint["permission_plan"]["runtime"]  # type: ignore[index]

    assert runtime["network"] == ["example.com"]  # type: ignore[index]
    assert runtime["codex"] == {"internet_access": True}  # type: ignore[index]


def test_product_manager_contract_keeps_schedules_only_for_services() -> None:
    service = ProductManagerContractService()
    schedule = {
        "type": "daily",
        "time": "08:00",
        "timezone": "America/Toronto",
        "input": {},
    }
    fallback = {
        "goal": "Daily cleanup",
        "runtime": "service",
        "skill_name": "daily_cleanup",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "schedule": schedule,
    }

    service_blueprint = service.sanitize_blueprint({}, fallback)
    function_blueprint = service.sanitize_blueprint(
        {"runtime": "function", "schedule": schedule},
        fallback,
    )

    assert service_blueprint["schedule"] == schedule
    assert function_blueprint["schedule"] is None


def test_codex_invocation_recorder_keeps_build_usage_buffer_separate() -> None:
    result = CompletedProcess(args=["codex"], returncode=0, stdout="", stderr="")
    result.codex_usage = {"input_tokens": 10, "total_tokens": 12}  # type: ignore[attr-defined]
    recorder = CodexInvocationRecorder(db=None)  # type: ignore[arg-type]

    recorder.record_build_result(
        result,
        {"codex_task": "builder"},
        default_adapter_name="test",
        prompt="exact prompt",
    )

    assert recorder.consume_build_usage() == [
        {
            "action": "builder",
            "adapter": "test",
            "status": "succeeded",
            "requested_model": None,
            "effective_model": None,
            "model": None,
            "requested_reasoning_effort": None,
            "effective_reasoning_effort": None,
            "route_source": None,
            "role": None,
            "difficulty": None,
            "cli_path": None,
            "cli_version": None,
            "cli_source": None,
            "thread_id": None,
            "turn_id": None,
            "input_tokens": 10,
            "total_tokens": 12,
        }
    ]
    assert recorder.consume_build_usage() == []
    assert recorder.consume_build_transcripts() == [
        {"action": "builder", "input": "exact prompt", "output": ""}
    ]
    assert recorder.consume_build_transcripts() == []


def test_agent_run_artifact_store_round_trips_controlled_json(tmp_path: Path) -> None:
    run = AgentRun(id=17, run_type="build_skill", status="running", user_request="Build it")
    store = AgentRunArtifactStore(None, tmp_path, TaskDagService())  # type: ignore[arg-type]

    store.initialize(run)
    relative_path = store.write_json(run, "blueprint.json", {"goal": "Build it"})

    assert relative_path == f"runtime/agent_runs/run_{run.id}/blueprint.json"
    assert store.read_json(run, "blueprint.json") == {"goal": "Build it"}
