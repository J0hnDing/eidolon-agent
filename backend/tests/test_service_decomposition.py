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
                "depends_on": [],
                "parallel_safe": True,
                "requires_tests": True,
                "expected_output_paths": ["alpha.py"],
                "file_write_claims": ["alpha.py"],
                "acceptance_criteria": ["alpha passes"],
            },
            {
                "id": "beta",
                "depends_on": [],
                "parallel_safe": True,
                "requires_tests": False,
                "expected_output_paths": ["beta.py"],
                "file_write_claims": ["beta.py"],
                "acceptance_criteria": ["beta passes"],
            },
            {
                "id": "merge",
                "depends_on": ["alpha", "beta"],
                "parallel_safe": False,
                "requires_tests": True,
                "expected_output_paths": ["skill.py"],
                "file_write_claims": ["skill.py"],
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
        "interface_type": "chat",
        "requested_permissions": {"filesystem_write": ["./cache", "report.json"]},
    }

    blueprint = service.sanitize_blueprint(
        {"goal": "Build it", "skill_name": "sample", "unknown": "drop"},
        fallback,
    )

    assert "unknown" not in blueprint
    assert blueprint["permission_plan"]["runtime"]["filesystem_write"] == ["report.json"]  # type: ignore[index]


def test_codex_invocation_recorder_keeps_build_usage_buffer_separate() -> None:
    result = CompletedProcess(args=["codex"], returncode=0, stdout="", stderr="")
    result.codex_usage = {"input_tokens": 10, "total_tokens": 12}  # type: ignore[attr-defined]
    recorder = CodexInvocationRecorder(db=None)  # type: ignore[arg-type]

    recorder.record_build_result(result, {"codex_task": "builder"}, default_adapter_name="test")

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
            "input_tokens": 10,
            "total_tokens": 12,
        }
    ]
    assert recorder.consume_build_usage() == []


def test_agent_run_artifact_store_round_trips_controlled_json(tmp_path: Path) -> None:
    run = AgentRun(id=17, run_type="build_skill", status="running", user_request="Build it")
    store = AgentRunArtifactStore(None, tmp_path, TaskDagService())  # type: ignore[arg-type]

    store.initialize(run)
    relative_path = store.write_json(run, "blueprint.json", {"goal": "Build it"})

    assert relative_path == f"runtime/agent_runs/run_{run.id}/blueprint.json"
    assert store.read_json(run, "blueprint.json") == {"goal": "Build it"}
