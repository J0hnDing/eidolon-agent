# Agent System Overview

The MVP uses bounded role-based agent workflows. Agents are not autonomous background actors and do not freely chat with each other.

## Roles

- ProductManagerAgent
- BuilderAgent
- TesterAgent

Permission review is deterministic backend logic and is not an agent role.

## Communication Model

Agents communicate through platform artifacts:

- `agent_runs`
- `agent_run_steps`
- `intent_prompt.json`
- `decision.json`
- `decision_json`
- `blueprint_json`
- `runtime/agent_runs/run_<id>/blueprint.json`
- `runtime/agent_runs/run_<id>/permissions.json`
- `runtime/agent_runs/run_<id>/task_dag.json`
- `runtime/agent_runs/run_<id>/tasks/<task_id>.json`
- `runtime/agent_runs/run_<id>/tasks/<task_id>/interface_artifact.json`
- `test_result_json`
- `failure_log`
- `runtime_permissions.json`
- `user_summary`

Build workflows use task-node artifacts instead of linear milestone artifacts. Each agent action should be visible as an `agent_run_steps` row with the action name, task node id when applicable, structured inputs, structured outputs, logs, and status.

## Codex Integration

ProductManager, Builder, and Tester are Codex-backed through `CodexService`. Project-build packages under `backend/app/workflows/` own and load their workflow-specific instructions and prompt composition. Shared preflight instructions live under `workflows/common`, DAG instructions under `workflows/task_dag`, and the one-call prompt under `workflows/single_codex`. Update and standalone repair actions continue to load role-relative files from `backend/app/agent_instructions/`.

Tests may use fake Codex adapters. Production workflow code should not bypass Codex for these roles except as a safe fallback when Codex output is unusable.

## Role Boundaries

Agents must not:

- install skills;
- run skills;
- approve permissions;
- install packages silently;
- mutate app source while building application skills;
- bypass manifest validation;
- bypass Tester failures;
- bypass backend permission checks.

## Failure Policy

For project build workflows, one task node can fail and repair up to three times. The final end-to-end test loop also has its own three-failure limit. After repeated failures, ProductManager stops the workflow and writes a user-facing stuck summary.
