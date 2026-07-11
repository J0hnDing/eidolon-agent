# Documentation Index

This folder contains the detailed functionality documentation for the local-first personal agent. `AGENTS.md` is intentionally thin and should remain focused on project vision, repository structure, and safety guardrails.

## Start Here

- [Architecture overview](architecture/overview.md)
- [Data model](architecture/data_model.md)
- [Backend API and services](backend/api_and_services.md)
- [Frontend UI behavior](frontend/ui.md)

## Skills

- [Skill overview](skills/overview.md)
- [Manifest and package contract](skills/manifest.md)
- [Skill lifecycle](skills/lifecycle.md)
- [Skill versioning](skills/versioning.md)

## Agent Workflows

- [Agent system overview](agents/overview.md)
- [ProductManagerAgent](agents/product_manager.md)
- [BuilderAgent](agents/builder.md)
- [TesterAgent](agents/tester.md)
- [Instruction files](agents/instruction_files.md)
- [Project build workflow](workflows/project_build_workflow.md)
- [Update workflow](workflows/update_workflow.md)

## Safety and Runtime

- [Permission system](security/permissions.md)
- [Sandbox execution](security/sandbox_execution.md)
- [Scheduling](runtime/scheduling.md)
- [Tools](runtime/tools.md)

## Integrations and History

- [Codex CLI integration](integrations/codex_cli.md)
- [Working history file](working_history.md)
- [TODO](todo.md)

## Documentation Rules

- Put detailed behavior here, not in `AGENTS.md`.
- Keep `AGENTS.md` as an entry point and safety guardrail file.
- When changing behavior, update the closest topic file in this folder.
- If a new subsystem grows large, create a subfolder rather than expanding a single long markdown file.
