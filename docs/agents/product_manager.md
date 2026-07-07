# ProductManagerAgent

ProductManagerAgent owns project judgment, intent refinement, blueprinting, permission-file drafting, DAG task planning, and user-facing blocked/clarification responses.

## Responsibilities

- Understand the user's request.
- Read backend-selected explicit user memory facts when they are relevant to the Project-mode request.
- Rewrite the request into a clearer build prompt before plausibility review.
- Decide whether the refined request is plausible, unclear, unsupported, or worth building without creating blueprint, permission, or task DAG artifacts.
- If the request is unclear, ask one user-facing clarification question and wait for the next Project-mode chat reply on the same generation request.
- If the request is infeasible or unsupported, explain why and stop without creating blueprint, permission, or task DAG artifacts.
- Write a concise blueprint without tasks or milestones.
- Draft build-time and expected runtime permission intent in a separate permission file.
- Define a task DAG for new skill builds after build-time approval.
- For each task node, define dependencies, difficulty, whether tests are required, expected inputs/outputs, file write claims, interface artifact expectations, and acceptance criteria.
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

For build workflows, ProductManager emits these artifacts in order:

```text
intent_prompt.json
decision.json
blueprint.json
permissions.json
task_dag.json
```

Backend state enforces this split:

- Before `proceed_to_blueprint`, `AgentWorkflowService` does not create a building skill and does not write `blueprint.json`, `permissions.json`, or `task_dag.json`.
- After `proceed_to_blueprint`, ProductManager writes `blueprint.json` and `permissions.json`.
- Backend performs deterministic build-time permission review and waits for user approval.
- Only after approval does ProductManager write `task_dag.json`.

Task node files describe product work only. They should not contain backend bookkeeping paths such as `blueprint_path`, `permission_path`, or artifact directory paths.

For tool skills, one or more task nodes must cover the declarative UI contract. Tool UI work means `tool_ui_schema`, input/output schemas, labels, fields, options/defaults, and acceptance criteria for Tools-page rendering. BuilderAgent must not create app frontend code for generated tools.

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
