import io
import json
import subprocess

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import AgentRun
from app.services.agent_workflow_service import AgentWorkflowService
from app.services.codex_service import CodexService, FakeCodexAdapter, RealCodexAdapter
from app.services.codex_usage_service import CodexUsageService


@pytest.fixture
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'usage.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_real_codex_adapter_parses_turn_usage() -> None:
    usage = RealCodexAdapter._usage_from_events(
        [
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 120,
                    "cached_input_tokens": 30,
                    "output_tokens": 40,
                    "reasoning_output_tokens": 10,
                },
            }
        ]
    )

    assert usage == {
        "input_tokens": 120,
        "cached_input_tokens": 30,
        "output_tokens": 40,
        "reasoning_output_tokens": 10,
        "total_tokens": 160,
    }


def test_model_catalog_normalizes_advertised_efforts() -> None:
    model = CodexUsageService._normalize_model(
        {
            "id": "gpt-example",
            "model": "gpt-example",
            "displayName": "GPT Example",
            "description": "Example",
            "isDefault": True,
            "defaultReasoningEffort": "medium",
            "supportedReasoningEfforts": [
                {"reasoningEffort": "low", "description": "Fast"},
                {"reasoningEffort": "medium", "description": "Balanced"},
            ],
        }
    )

    assert model["model"] == "gpt-example"
    assert model["default_reasoning_effort"] == "medium"
    assert model["supported_reasoning_efforts"] == ["low", "medium"]


def test_codex_usage_service_normalizes_primary_and_secondary_windows(monkeypatch) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = io.StringIO()
            self.stdout = io.StringIO(
                json.dumps({"id": 1, "result": {"userAgent": "test"}})
                + "\n"
                + json.dumps(
                    {
                        "id": 2,
                        "result": {
                            "rateLimits": {},
                            "rateLimitsByLimitId": {
                                "codex": {
                                    "limitId": "codex",
                                    "planType": "plus",
                                    "primary": {"usedPercent": 20, "windowDurationMins": 300, "resetsAt": 1_800_000_000},
                                    "secondary": {"usedPercent": 35, "windowDurationMins": 10080, "resetsAt": 1_800_100_000},
                                    "rateLimitReachedType": None,
                                }
                            },
                        },
                    }
                )
                + "\n"
            )

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    service = CodexUsageService("codex")
    service.start()
    usage = service.read_account_usage()

    assert usage["available"] is True
    assert usage["five_hour"]["remaining_percent"] == 80
    assert usage["five_hour"]["window_duration_minutes"] == 300
    assert usage["weekly"]["remaining_percent"] == 65
    assert usage["weekly"]["window_duration_minutes"] == 10080


def test_finished_step_persists_invocations_and_rolls_up_tokens(db_session) -> None:
    run = AgentRun(run_type="build_skill", status="running", user_request="Build it")
    db_session.add(run)
    db_session.commit()
    codex = CodexService(db_session, adapter=FakeCodexAdapter())
    workflow = AgentWorkflowService(db_session, codex_service=codex)
    step = workflow._start_step(run, "builder", milestone_name="core_skill")
    codex._pending_invocations.append(
        {
            "action": "builder_build_task",
            "adapter": "test_adapter",
            "model": "test-model",
            "input_tokens": 100,
            "cached_input_tokens": 20,
            "output_tokens": 25,
            "reasoning_output_tokens": 5,
            "total_tokens": 125,
        }
    )

    workflow._finish_step(run, step, "succeeded")

    assert step.total_tokens == 125
    assert step.codex_invocations_json[0]["adapter"] == "test_adapter"
    assert run.total_input_tokens == 100
    assert run.total_tokens == 125


def test_workflow_pauses_when_either_codex_window_is_below_five_percent(db_session, monkeypatch) -> None:
    class QuotaAdapter(FakeCodexAdapter):
        uses_codex_account_quota = True

    run = AgentRun(run_type="build_skill", status="running", user_request="Build it")
    db_session.add(run)
    db_session.commit()
    workflow = AgentWorkflowService(db_session, codex_service=CodexService(db_session, adapter=QuotaAdapter()))
    usage = {
        "available": True,
        "five_hour": {"label": "5-hour", "remaining_percent": 4, "resets_at": "2030-01-01T00:00:00Z"},
        "weekly": {"label": "weekly", "remaining_percent": 50, "resets_at": None},
    }
    monkeypatch.setattr(
        "app.services.agent_workflow_service.codex_usage_service.should_pause_workflow",
        lambda minimum_remaining_percent: (True, usage),
    )

    assert workflow._pause_if_usage_below_reserve(run) is True
    assert run.status == "paused"
    assert "5-hour" in (run.pause_reason or "")
    assert "less than 5%" in (run.pause_reason or "")
    assert (run.final_summary_json or {})["usage_pause"] == usage


def test_five_percent_remaining_does_not_pause() -> None:
    service = CodexUsageService("codex")
    service.read_account_usage = lambda: {  # type: ignore[method-assign]
        "available": True,
        "rate_limit_reached_type": None,
        "five_hour": {"remaining_percent": 5},
        "weekly": {"remaining_percent": 5},
    }

    should_pause, _usage = service.should_pause_workflow(5)

    assert should_pause is False


@pytest.mark.parametrize(("five_hour_remaining", "weekly_remaining"), [(4, 50), (50, 4)])
def test_either_allowance_window_below_five_percent_pauses(
    five_hour_remaining: int,
    weekly_remaining: int,
) -> None:
    service = CodexUsageService("codex")
    service.read_account_usage = lambda: {  # type: ignore[method-assign]
        "available": True,
        "rate_limit_reached_type": None,
        "five_hour": {"remaining_percent": five_hour_remaining},
        "weekly": {"remaining_percent": weekly_remaining},
    }

    should_pause, _usage = service.should_pause_workflow(5)

    assert should_pause is True


def test_parallel_safe_nodes_share_a_ready_batch(db_session) -> None:
    workflow = AgentWorkflowService(db_session, codex_service=CodexService(db_session, adapter=FakeCodexAdapter()))
    task_dag = {
        "nodes": [
            {"id": "left", "depends_on": [], "parallel_safe": True},
            {"id": "right", "depends_on": [], "parallel_safe": True},
            {"id": "join", "depends_on": ["left", "right"], "parallel_safe": True},
        ]
    }

    batches = workflow._task_execution_batches(task_dag)

    assert [[node["id"] for node in batch] for batch in batches] == [["left", "right"], ["join"]]
