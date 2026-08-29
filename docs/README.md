# Eidolon Documentation

This folder is the detailed source of truth for current behavior, confirmed future work, and concise implementation history. `AGENTS.md` remains the high-level entry point and safety contract.

## Compatibility Identifiers

The product and repository are named **Eidolon**. Existing environment variables with the `PERSONAL_AGENT_*` prefix, the `personal_agent.db` SQLite filename, browser-storage keys, internal gateway routes, and some Docker resource names remain stable compatibility contracts so the rename does not orphan local configuration, history, or installed skills. New user-facing copy and package metadata use Eidolon.

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
- [Sandboxed web applications](runtime/web_applications.md)
- [Function registry and invocation](runtime/functions.md)
- [Extending the function catalog](runtime/function_extension_guide.md)
- [Scheduling](runtime/scheduling.md)

## Integrations, Roadmap, and History

- [GitHub integration capability](integrations/github.md)
- [Eidolon-Atlas integration](integrations/atlas.md)
- [Notion Todo and Reports integration](integrations/notion.md)
- [Google Calendar OAuth integration](integrations/google_calendar.md)
- [Codex CLI integration](integrations/codex_cli.md)
- [Codex MCP tools](integrations/codex_mcp.md)
- [Working history file](working_history.md)
- [TODO](todo.md)

## Documentation Rules

- Put current detailed behavior in the closest topic file; do not repeat the same contract across several pages.
- Keep `AGENTS.md` focused on project-wide guardrails, navigation, TODO rules, and verification requirements.
- Keep confirmed unfinished work in `todo.md` and completed behavior in `working_history.md`; neither file replaces current-behavior documentation.
- Update code, tests, and the closest topic document together when behavior changes.
- Split a growing subsystem into a focused subfolder instead of turning one page into a catch-all.
