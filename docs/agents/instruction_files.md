# Agent Instruction Files

Agent behavior is defined by Markdown instruction files in:

```text
backend/app/agent_instructions/
```

Current files:

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

## Editing Rule

When changing agent behavior, update the relevant instruction file and the matching documentation under `docs/agents/` or `docs/workflows/`.
