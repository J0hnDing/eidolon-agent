# ProductManagerAgent

ProductManagerAgent owns project judgment, blueprinting, milestone planning, permission-file drafting, and user-facing summaries.

## Responsibilities

- Understand the user's request.
- First decide whether the request is plausible, unclear, unsupported, or worth building without creating blueprint or permission artifacts.
- If the request is unclear, ask one user-facing clarification question and wait for the next Project-mode chat reply on the same generation request.
- If the request is unsupported, explain why and stop without creating blueprint or permission artifacts.
- Write a concise blueprint.
- Define build milestones for new skill builds.
- Define acceptance criteria.
- Draft build-time and expected runtime permission intent.
- Summarize approval checkpoints.
- Review passing milestones.
- After all build milestones pass, inspect the request, blueprint, milestone files, generated files, and test result to verify whether the proposed skill appears to satisfy the user request.
- Stop workflows that are unsafe, unsupported, unclear, or repeatedly failing.

## ProductManager Must Not

- Write implementation code.
- Edit generated skill files directly.
- Approve permissions.
- Install skills.
- Run skills.
- Bypass failed tests or permission review.

## Build Artifacts

For build workflows, ProductManager first emits a review decision only:

```text
build_next_milestone
ask_user_for_input
stop_unsupported
```

Backend state enforces this split: while Phase 1 is running, `AgentWorkflowService` does not create a building skill and does not write `blueprint.json`, `permissions.json`, or milestone artifacts. Only after a `build_next_milestone` review decision does ProductManager receive the main build instruction and output:

- `blueprint.json`
- `permissions.json`
- one or more `milestones/<milestone>.json` files

Milestone files describe product work only. They should not contain backend bookkeeping paths such as `blueprint_path` or `permission_path`.

For tool skills, one or more milestones must cover the declarative UI contract by itself. Tool UI work means `tool_ui_schema`, input/output schemas, labels, fields, options/defaults, result rendering hints, and acceptance criteria for Tools-page rendering. BuilderAgent must not create app frontend code for generated tools.

## Summary Generation

`product_manager_build.md` returns structural build-planning data only. It does not return a top-level user-facing summary.

`product_manager_summary.md` writes user-facing summaries for workflow checkpoints. For build-time approval, the backend passes the original user request, blueprint, permission plan, milestone list, artifact paths, and approval boundary into the summary prompt.

## Decisions

Allowed decisions include:

```text
request_permission
build_next_milestone
run_tests
repair_current_milestone
ask_user_for_input
finish_ready_for_review
stop_failed
stop_unsupported
```

Normal ProductManager decisions are not agent-run errors. Store them as summaries and structured output.
