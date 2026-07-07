# Agent Instruction Files

Agent behavior is defined by Markdown instruction files in:

```text
backend/app/agent_instructions/
```

Current files:

- `product_manager_plausibility_review.md`
- `product_manager_build.md`
- `product_manager_repair.md`
- `product_manager_update.md`
- `product_manager_summary.md`
- `builder_instruction_build.md`
- `builder_instruction_repair.md`
- `builder_instruction_update.md`
- `tester_instruction_build.md`
- `tester_instruction_update.md`

The DAG build refactor should either update these build instruction files or split them into action-specific files:

- ProductManager: intent refinement, plausibility decision, blueprint, permissions, task DAG.
- Builder: build task node, fix task node, fix final end-to-end failure.
- Tester: test task node, final end-to-end test.

## How They Are Used

`CodexService` loads the relevant instruction file, combines it with structured context, and sends the prompt to the configured Codex adapter.

Instruction files should define role behavior and constraints. Backend code should coordinate state, validate outputs, and enforce safety.

`product_manager_plausibility_review.md` is used for the build plausibility action and returns only an intent/plausibility decision. It must not receive blueprint instructions or return blueprint, permission, or task DAG artifacts.

`product_manager_build.md` currently covers multiple post-review build responsibilities. In the DAG workflow, those responsibilities are logically separate: blueprint creation, permission planning, and task DAG creation. Backend steps should keep those outputs separate even if one instruction file temporarily implements more than one action during migration.

## Editing Rule

When changing agent behavior, update the relevant instruction file and the matching documentation under `docs/agents/` or `docs/workflows/`.
