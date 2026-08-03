# Agent Instruction Files

Agent behavior is defined by Markdown instruction files owned by either a workflow package or a non-project-build role action.

Project-build instructions are colocated with their workflow code:

```text
backend/app/workflows/
  common/
    prompts.py
    instructions/
      refine_intent.md
      plausibility_review.md
      blueprint_and_permissions.md
  task_dag/
    workflow.py
    prompts.py
    instructions/
      product_manager.md
      builder.md
      repair.md
      tester.md
  single_codex/
    workflow.py
    prompts.py
    instructions/
      run.md
```

Instructions for update and standalone repair actions remain grouped by agent role until those workflows are modularized:

```text
backend/app/agent_instructions/
  product_manager/
    repair.md
    update.md
  builder/
    repair.md
    update.md
  tester/
    repair.md
    update.md
```

The shared project-build preflight uses `workflows/common`. It owns ProductManager intent refinement, plausibility review, and blueprint/permission/workflow selection before post-approval dispatch.

The DAG build workflow uses the instructions under `workflows/task_dag`:

- ProductManager: task DAG planning after approval.
- Builder: build task node, fix task node, fix final end-to-end failure.
- Tester: test task node, final end-to-end test.

The single-Codex workflow uses `workflows/single_codex/instructions/run.md` for its one planning, build, and test invocation.

## How They Are Used

Each project-build package loads its own instruction files and composes its own prompts. `CodexService` supplies shared Codex invocation, parsing, routing, usage, workspace, and safety primitives. Update and standalone repair prompts continue to use role-relative files under `backend/app/agent_instructions/`.

Instruction files define role behavior and how to consume supplied permission context. Permission classifications must not be copied into them: `backend/app/static/default_permissions.json` is the source of truth for `default_allowed`, `requires_approval`, and `blocked`. Backend code loads that policy, coordinates state, validates outputs, and enforces safety.

`workflows/common/instructions/refine_intent.md` is used for `pm_refine_intent` and returns only `intent_prompt.json`. It must not make plausibility decisions, ask clarification questions, or return blueprint, permission, or task DAG artifacts.

`workflows/common/instructions/plausibility_review.md` is the single project-build plausibility prompt. It returns only an intent/plausibility decision and receives the config-derived `blocked` field, not default or approval-required policy. It must not receive blueprint instructions or return blueprint, permission, or task DAG artifacts.

`workflows/common/instructions/blueprint_and_permissions.md` is used for `pm_write_blueprint_and_permissions`. It receives the full config-derived `permission_policy`; the backend parses the response into backend-only workflow selection plus separate blueprint and permission-plan artifacts.

`workflows/task_dag/instructions/product_manager.md` is used only after build-time approval for `pm_write_task_dag`.

The common ProductManager blueprint instruction receives the concise available-only unified function-catalog index. Task-DAG Builder/Tester instructions consume full catalog context only for function ids assigned to the current node. The single-Codex instruction receives full context for every blueprint-selected function. Update and repair role instructions preserve the same selected-only boundary.

ProductManager summary calls currently use deterministic backend fallback summaries instead of a separate PM summary instruction file.

## Editing Rule

When changing agent behavior, update the instruction owned by the relevant workflow or role action and the matching documentation under `docs/agents/` or `docs/workflows/`.
