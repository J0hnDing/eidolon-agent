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

## How They Are Used

`CodexService` loads the relevant instruction file, combines it with structured context, and sends the prompt to the configured Codex adapter.

Instruction files should define role behavior and constraints. Backend code should coordinate state, validate outputs, and enforce safety.

`product_manager_plausibility_review.md` is used for the build `build_review` task and returns only an intent/plausibility decision. It must not receive blueprint instructions or return blueprint artifacts.

`product_manager_build.md` is used only after the review passes. It receives the fuller build context and returns blueprint plus permission-planning content. It does not return top-level user-facing summary text.

`product_manager_summary.md` is used for user-facing checkpoint summaries. Backend services feed the relevant structured context into the prompt, including build-time blueprint and permission context, milestone/test context, runtime permission context, repair context, or update context depending on `summary_type`.

## Editing Rule

When changing agent behavior, update the relevant instruction file and the matching documentation under `docs/agents/` or `docs/workflows/`.
