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
- `blueprint_json`
- `runtime/agent_runs/run_<id>/blueprint.json`
- `runtime/agent_runs/run_<id>/permissions.json`
- `runtime/agent_runs/run_<id>/milestones/<name>.json`
- `decision_json`
- `test_result_json`
- `failure_log`
- `runtime_permissions.json`
- `user_summary`

## Codex Integration

ProductManager, Builder, and Tester are Codex-backed through `CodexService`. Each action loads an instruction file from `backend/app/agent_instructions/`.

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

For build and repair workflows, one milestone can fail and repair up to three times. After repeated failures, ProductManager stops the workflow and writes a user-facing stuck summary.
