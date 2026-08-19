# ProductManagerAgent

ProductManagerAgent owns project judgment, build-workflow selection, blueprinting, permission-file drafting, conditional DAG task planning, and user-facing blocked/clarification responses. The preceding intent step is currently a backend passthrough placeholder, not a ProductManagerAgent invocation.

## Responsibilities

- Understand the user's request.
- In the resumable `pm_plan_build` session, decide whether the refined request needs clarification, is unsupported, or can proceed to approval.
- On the first planning turn, use the available function catalog and complete canonical permission policy supplied by the backend.
- If the request is unclear, ask one user-facing clarification question and wait for the next Project-mode chat reply on the same generation request.
- If the request is infeasible or unsupported, explain why and stop without creating blueprint, permission, or task DAG artifacts.
- Write a concise blueprint without tasks or milestones.
- Use one filesystem-safe `name` and one concise `description`; the backend derives presentation labels.
- Select `runtime = function` for bounded JSON stdin/stdout execution or `runtime = web_app` for a self-rendered interactive ASGI application.
- For function skills, define the complete object-shaped input and output JSON Schemas in the blueprint.
- Select every needed backend-core, user, or integration function by exact id from the backend-provided available catalog. Function selection has no reason field.
- Describe concrete user-visible requirements in `expected_behavior`. Blueprint-level acceptance criteria are not part of the contract; task-specific criteria are created later for Task DAG nodes.
- Do not return integration scope objects. The backend derives providers and operations from selected function ids, while generated-manifest runtime approval owns provider-specific resource authorization.
- Include intended recurring schedule metadata only for function skills. Web applications use `schedule = null` because their service lifetime is not a scheduled bounded run.
- Draft build-time and expected runtime permission intent in a separate permission file.
- During blueprint or update planning, use the complete config-derived `permission_policy`: omit `default_allowed`, return the exact `requires_approval` shape, and reject needs listed in `blocked`.
- Return one top-level `build_workflow` value: `single_codex` for a self-contained small or medium build, or `task_dag` when explicit dependency boundaries and independently retryable tasks are needed.
- Keep `build_workflow` outside the blueprint because it is backend routing state and must not be written to `blueprint.json`.
- Define a task DAG after build-time approval only when `build_workflow=task_dag`.
- For each task node, define a direct task prompt, dependencies, difficulty, whether tests are required, required write paths, acceptance criteria, and test expectations.
- For Task DAG builds, assign only blueprint-selected functions to the nodes that use them through `function_ids`.
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

- The deterministic `pm_refine_intent` placeholder runs once for the initial Project-mode request and writes that message unchanged to `intent_prompt.json`; it does not invoke ProductManagerAgent or Codex. Clarification replies resume the same planning session without rerunning the placeholder.
- Downstream PM prompts receive only the passthrough prompt text.

- Before `proceed_to_approval`, `AgentWorkflowService` does not create a building skill and does not write `blueprint.json`, `permissions.json`, or `task_dag.json`; it writes `decision.json` after each planning turn.
- `pm_plan_build` always returns `decision`, `user_prompt`, `build_workflow`, `blueprint`, and `permission_plan`. Clarification and rejection responses contain no planning artifacts; `proceed_to_approval` contains the complete workflow, blueprint, and permission plan, which the backend then writes.
- A persistent Codex App Server thread is resumed for each clarification reply and archived after rejection, approval handoff, or cancellation.
- Backend performs deterministic build-time permission review and waits for user approval.
- After approval the backend resolves the registered workflow. Only `task_dag` invokes ProductManager again to return task DAG JSON and write `task_dag.json`; `single_codex` invokes Codex once with the blueprint and effective permission bounds.
- Task-DAG planning receives compact backend-approved permission bounds, including config-derived `blocked`, rather than duplicated policy prose.
- Backend derives the initial package `manifest.json` from `blueprint.json` and `permissions.json`; ProductManager does not write generated skill files directly. If ProductManager included schedule intent in the blueprint, the manifest skeleton carries it into `manifest.json`.

Schedule intent in `blueprint.json` uses the manifest schedule shape. Use `null` when the user did not request recurrence. For recurrence, use one of:

- daily: `{"type": "daily", "time": "09:00", "timezone": "America/Toronto", "input": {}}`;
- weekly: `{"type": "weekly", "day": "monday", "time": "09:00", "timezone": "America/Toronto", "input": {}}`;
- interval: `{"type": "interval", "every": 1, "unit": "hours", "timezone": "America/Toronto", "input": {}}`.

Task node files describe product work only. They should not contain backend bookkeeping paths such as `blueprint_path`, `permission_path`, or artifact directory paths.

The backend automatically supplies ProductManager with the available function-catalog index. ProductManager does not call a discovery endpoint. The index contains only the exact id, title, description, category, and risk needed for selection; it omits schemas, endpoints, authentication behavior, settings routes, credential state, and secret-store details.

The blueprint's `functions` list is the authoritative selection. For a Task DAG, every node's `function_ids` must be a subset of that list and every selected function must be assigned to at least one node. The backend resolves each node's ids into full Builder context. For `single_codex`, it supplies full context for every blueprint-selected function. Scheduling is manifest metadata, not a catalog function.

For a web application, ProductManager may assign package-owned Python/HTML/CSS/JavaScript work but must never assign Eidolon frontend files, custom Dockerfiles, or startup commands. Function skills keep the bounded JSON protocol and do not receive interface-specific task nodes.

## Decisions

Allowed decisions include:

```text
request_permission
proceed_to_approval
run_tests
repair_current_task
ask_user_for_input
finish_project
stop_failed
stop_inplausible
```

Normal ProductManager decisions are not agent-run errors. Store them as summaries and structured output.
