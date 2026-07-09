# Agent Instruction Files

Agent behavior is defined by Markdown instruction files in:

```text
backend/app/agent_instructions/
```

Instruction files are grouped by agent role:

```text
backend/app/agent_instructions/
  product_manager/
    refine_intent.md
    plausibility_review.md
    blueprint_and_permissions.md
    task_dag.md
    build.md
    repair.md
    update.md
  builder/
    build.md
    repair.md
    update.md
  tester/
    build.md
    update.md
```

The DAG build workflow uses action-specific ProductManager instruction files:

- ProductManager: intent refinement, plausibility decision, blueprint, permissions, task DAG.
- Builder: build task node, fix task node, fix final end-to-end failure.
- Tester: test task node, final end-to-end test.

## How They Are Used

`CodexService` loads the relevant instruction file, combines it with structured context, and sends the prompt to the configured Codex adapter.

Instruction files should define role behavior and constraints. Backend code should coordinate state, validate outputs, and enforce safety.

`product_manager/refine_intent.md` is used for `pm_refine_intent` and returns only `intent_prompt.json`. It must not make plausibility decisions, ask clarification questions, or return blueprint, permission, or task DAG artifacts.

`product_manager/plausibility_review.md` is used for the build plausibility action and returns only an intent/plausibility decision. It must not receive blueprint instructions or return blueprint, permission, or task DAG artifacts.

`product_manager/blueprint_and_permissions.md` is used for `pm_write_blueprint_and_permissions`. The backend parses the single response into separate blueprint and permission-plan artifacts.

`product_manager/task_dag.md` is used only after build-time approval for `pm_write_task_dag`.

`product_manager/build.md` remains as a legacy instruction file and is not used by the current new-skill build path.

ProductManager summary calls currently use deterministic backend fallback summaries instead of a separate PM summary instruction file.

## Editing Rule

When changing agent behavior, update the relevant instruction file and the matching documentation under `docs/agents/` or `docs/workflows/`.
