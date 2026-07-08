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
    build.md
    repair.md
    update.md
    summary.md
  builder/
    build.md
    repair.md
    update.md
  tester/
    build.md
    update.md
```

The DAG build refactor should either update these build instruction files or split them into action-specific files:

- ProductManager: intent refinement, plausibility decision, blueprint, permissions, task DAG.
- Builder: build task node, fix task node, fix final end-to-end failure.
- Tester: test task node, final end-to-end test.

## How They Are Used

`CodexService` loads the relevant instruction file, combines it with structured context, and sends the prompt to the configured Codex adapter.

Instruction files should define role behavior and constraints. Backend code should coordinate state, validate outputs, and enforce safety.

`product_manager/refine_intent.md` is used for `pm_refine_intent` and returns only `intent_prompt.json`. It must not make plausibility decisions, ask clarification questions, or return blueprint, permission, or task DAG artifacts.

`product_manager/plausibility_review.md` is used for the build plausibility action and returns only an intent/plausibility decision. It must not receive blueprint instructions or return blueprint, permission, or task DAG artifacts.

`product_manager/build.md` currently covers multiple post-review build responsibilities. In the DAG workflow, those responsibilities are logically separate: blueprint creation, permission planning, and task DAG creation. Backend steps should keep those outputs separate even if one instruction file temporarily implements more than one action during migration.

## Editing Rule

When changing agent behavior, update the relevant instruction file and the matching documentation under `docs/agents/` or `docs/workflows/`.
