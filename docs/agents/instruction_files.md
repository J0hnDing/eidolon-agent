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

Instruction files should define role behavior and constraints. Backend code should coordinate state, validate outputs, and enforce safety.

`workflows/common/instructions/refine_intent.md` is used for `pm_refine_intent` and returns only `intent_prompt.json`. It must not make plausibility decisions, ask clarification questions, or return blueprint, permission, or task DAG artifacts.

`workflows/common/instructions/plausibility_review.md` is used for the build plausibility action and returns only an intent/plausibility decision. It must not receive blueprint instructions or return blueprint, permission, or task DAG artifacts.

`workflows/common/instructions/blueprint_and_permissions.md` is used for `pm_write_blueprint_and_permissions`. The backend parses the response into backend-only workflow selection plus separate blueprint and permission-plan artifacts.

`workflows/task_dag/instructions/product_manager.md` is used only after build-time approval for `pm_write_task_dag`.

The common ProductManager instructions receive only the concise GitHub operation index. Task-DAG Builder/Tester instructions and the single-Codex instruction consume registry-derived context only for approved operation ids. Update and repair role instructions preserve the same selected-only boundary.

ProductManager summary calls currently use deterministic backend fallback summaries instead of a separate PM summary instruction file.

## Editing Rule

When changing agent behavior, update the instruction owned by the relevant workflow or role action and the matching documentation under `docs/agents/` or `docs/workflows/`.
