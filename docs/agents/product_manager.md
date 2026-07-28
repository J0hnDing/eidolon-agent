# ProductManagerAgent

ProductManagerAgent owns project judgment, intent refinement, build-workflow selection, blueprinting, permission-file drafting, conditional DAG task planning, and user-facing blocked/clarification responses.

## Responsibilities

- Understand the user's request.
- Read backend-selected explicit user memory facts when they are relevant to the Project-mode request.
- Rewrite the request into a clearer build prompt before plausibility review.
- Decide whether the refined request is plausible, unclear, unsupported, or worth building without creating blueprint, permission, or task DAG artifacts.
- If the request is unclear, ask one user-facing clarification question and wait for the next Project-mode chat reply on the same generation request.
- If the request is infeasible or unsupported, explain why and stop without creating blueprint, permission, or task DAG artifacts.
- Write a concise blueprint without tasks or milestones.
- Select `runtime = function` for bounded JSON stdin/stdout execution or `runtime = web_app` for a self-rendered interactive ASGI application.
- Include intended recurring schedule metadata only for function skills. Web applications use `schedule = null` because their service lifetime is not a scheduled bounded run.
- Draft build-time and expected runtime permission intent in a separate permission file.
- Return one top-level `build_workflow` value: `single_codex` for a self-contained small or medium build, or `task_dag` when explicit dependency boundaries and independently retryable tasks are needed.
- Keep `build_workflow` outside the blueprint because it is backend routing state and must not be written to `blueprint.json`.
- Define a task DAG after build-time approval only when `build_workflow=task_dag`.
- For each task node, define dependencies, difficulty, whether tests are required, expected outputs, file write claims, interface artifact expectations, and acceptance criteria.
- For each task node, include `backend_api_ids` only when the node needs a backend API from the backend-provided API index.
- Select GitHub authorization intent only from the concise registry-derived integration index. For Task DAG builds, assign only the required selected ids through `integration_operation_ids`.
- Keep tightly coupled implementation work together when separate nodes would repeatedly edit the same code file without a meaningful interface boundary.
- Summarize approval checkpoints.
- Write stuck summaries when a node or final end-to-end loop exceeds failure limits.
- Stop workflows that are unsafe, unsupported, unclear, or repeatedly failing.

## ProductManager Must Not

- Write implementation code.
- Edit generated skill files directly.
- Approve permissions.
- Install skills.
- Run skills.
- Bypass failed tests or permission review.

## Build Artifacts

For build workflows, ProductManager returns structured JSON for these artifacts in order. The backend parses that JSON and writes the artifact files under `runtime/agent_runs/run_<id>/`:

```text
intent_prompt.json
decision.json
blueprint.json
permissions.json
task_dag.json  # task_dag workflow only
```

Backend state enforces this split:

- ProductManager refinement runs after every Project-mode user input and writes `intent_prompt.json`, including for first-turn requests with no clarification history or selected memory facts.
- Memory facts used during refinement remain auditable in `intent_prompt.json`, while downstream PM prompts receive only the refined prompt text.

- Before `proceed_to_blueprint`, `AgentWorkflowService` does not create a building skill and does not write `blueprint.json`, `permissions.json`, or `task_dag.json`.
- After `proceed_to_blueprint`, ProductManager returns one response containing `build_workflow`, blueprint, and permission-plan fields. The backend stores `build_workflow` on the agent run and writes only `blueprint.json` and `permissions.json`.
- Backend performs deterministic build-time permission review and waits for user approval.
- After approval the backend resolves the registered workflow. Only `task_dag` invokes ProductManager again to return task DAG JSON and write `task_dag.json`; `single_codex` invokes Codex once with the blueprint and effective permissions.
- Task-DAG planning receives compact backend-approved permission bounds rather than default policy and banned-permission prose duplicated from `permissions.json`.
- Backend derives the initial package `manifest.json` from `blueprint.json` and `permissions.json`; ProductManager does not write generated skill files directly. If ProductManager included schedule intent in the blueprint, the manifest skeleton carries it into `manifest.json`.

Schedule intent in `blueprint.json` uses the manifest schedule shape. Use `null` when the user did not request recurrence. For recurrence, use one of:

- daily: `{"type": "daily", "time": "09:00", "timezone": "America/Toronto", "input": {}}`;
- weekly: `{"type": "weekly", "day": "monday", "time": "09:00", "timezone": "America/Toronto", "input": {}}`;
- interval: `{"type": "interval", "every": 1, "unit": "hours", "timezone": "America/Toronto", "input": {}}`.

Task node files describe product work only. They should not contain backend bookkeeping paths such as `blueprint_path`, `permission_path`, or artifact directory paths.

Task node `backend_api_ids` are numeric references to backend APIs. ProductManager sees only id, title, and description. The backend resolves those ids into detailed Builder context before the node is built. Scheduling is not represented as a backend API id; it is manifest metadata.

GitHub operation selection follows the same minimal-context rule. ProductManager sees only operation id, title, and description—never schemas, endpoints, authentication behavior, settings routes, credential state, or secret-store details. The blueprint owns `integration_requirements`; a task node's `integration_operation_ids` must be a subset of the approved blueprint.

For a web application, ProductManager may assign package-owned Python/HTML/CSS/JavaScript work but must never assign Eidolon frontend files, custom Dockerfiles, or startup commands. Function skills keep the bounded JSON protocol and do not receive interface-specific task nodes.

## Decisions

Allowed decisions include:

```text
request_permission
proceed_to_blueprint
run_tests
repair_current_task
ask_user_for_input
finish_project
stop_failed
stop_inplausible
```

Normal ProductManager decisions are not agent-run errors. Store them as summaries and structured output.
