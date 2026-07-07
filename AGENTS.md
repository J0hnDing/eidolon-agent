# AGENTS.md

## Project

Local-First Self-Extending Personal AI Assistant.

This repository builds a local-first assistant that can chat with the user, store explicit editable memory, and turn repeated needs into safe reusable application skills. A skill is a reusable capability package that can be proposed, inspected, validated, installed, enabled, disabled, updated, scheduled, run, or deleted by the user.

Skill types:

- `instruction`: reusable instructions only, no executable code.
- `automation`: executable Python automation code.
- `hybrid`: reusable instructions plus executable Python automation.

User-facing tools are not a separate skill type. A tool is an installed, enabled automation or hybrid skill with `interface_type = "tool"` and optional declarative `tool_ui_schema`.

## Current Stack

- Backend: FastAPI, SQLite, SQLAlchemy, Pydantic schemas, pytest.
- Frontend: React, Vite, TypeScript, plain CSS.
- Skill language: Python.
- Skill execution: Docker sandbox by default when available, explicit local/dev fallback.
- Scheduling: APScheduler.
- Skill generation and agents: Codex CLI through backend service adapters.

## Repository Structure

```text
personal-agent/
  AGENTS.md                  high-level repo guidance and guardrails
  README.md
  docs/                      detailed project documentation
    README.md                documentation index
    working_history.md       future working-history log format, currently no history entries
    architecture/
    agents/
    backend/
    frontend/
    security/
    skills/
    workflows/
    runtime/
    integrations/
  backend/
    app/
      agent_instructions/    instruction files passed to Codex-backed agents
      models/                SQLAlchemy entities
      routers/               FastAPI routes
      schemas/               Pydantic schemas and manifest schema
      services/              workflow, permission, runner, Codex, scheduler services
    tests/
  frontend/
    src/
      pages/
      components/
      api/
      lib/
  skills/
    proposed/
    installed/
  runtime/                   local runtime artifacts, logs, cache, agent artifacts
```

Read the relevant files under `docs/` before making non-trivial changes. Start at [docs/README.md](docs/README.md).

## Documentation Map

- Product architecture: [docs/architecture/overview.md](docs/architecture/overview.md)
- Data model: [docs/architecture/data_model.md](docs/architecture/data_model.md)
- Backend routes and services: [docs/backend/api_and_services.md](docs/backend/api_and_services.md)
- Frontend behavior: [docs/frontend/ui.md](docs/frontend/ui.md)
- Skill concept and manifest: [docs/skills/overview.md](docs/skills/overview.md), [docs/skills/manifest.md](docs/skills/manifest.md)
- Skill lifecycle and versioning: [docs/skills/lifecycle.md](docs/skills/lifecycle.md), [docs/skills/versioning.md](docs/skills/versioning.md)
- Agent workflows: [docs/agents/overview.md](docs/agents/overview.md), [docs/workflows/project_build_workflow.md](docs/workflows/project_build_workflow.md), [docs/workflows/update_workflow.md](docs/workflows/update_workflow.md)
- Permissions and sandboxing: [docs/security/permissions.md](docs/security/permissions.md), [docs/security/sandbox_execution.md](docs/security/sandbox_execution.md)
- Scheduling and tools: [docs/runtime/scheduling.md](docs/runtime/scheduling.md), [docs/runtime/tools.md](docs/runtime/tools.md)
- Codex CLI integration: [docs/integrations/codex_cli.md](docs/integrations/codex_cli.md)
- Working history log format: [docs/working_history.md](docs/working_history.md)

## Non-Negotiable Guardrails

### Local First

The MVP is a local single-user system. Do not introduce multi-user SaaS assumptions, external account dependence, or cloud-only behavior unless explicitly requested.

### User Owns Memory

Memory must be explicit, editable, and deletable. Store actionable facts only. Do not silently collect all chat content or store sensitive personal data unless the user explicitly asks.

### No Silent Skill Creation

Normal chat mode must not create application skills. Project mode is the only UI mode that may start application skill proposal workflows. Do not use backend keyword heuristics to silently convert normal chat into skill generation.

### Platform Controls Generated Code

Codex may generate application skill files, tests, manifests, and documentation, but the application controls:

- where generated files are written,
- which permissions are declared,
- whether manifests validate,
- whether tests pass,
- whether runtime permissions are approved,
- whether a skill is installed, enabled, run, scheduled, updated, or deleted.

Generated skill files must stay inside controlled skill folders:

```text
skills/proposed/<skill_name>/
skills/installed/<skill_name>/versions/vN/
```

### Approval Boundaries

Approval to generate is not approval to install. Approval to install is not approval to run automatically. Schedule approval is not runtime permission approval. Runtime approval must be based on the generated manifest, not only the initial plan.

Approval prompts should be clear about:

- what is requested,
- why it is needed,
- risk level,
- unsupported or blocked items,
- what approval allows,
- what approval does not allow.

### Permission Limits

The MVP blocks or rejects:

- shell access,
- secrets,
- arbitrary filesystem reads,
- broad filesystem writes,
- unrestricted or wildcard network access,
- browser automation,
- email/calendar/finance actions,
- public posting,
- purchases,
- trading,
- file deletion,
- arbitrary command execution.

Reading and writing a skill's own `./cache` directory is allowed as low-risk local runtime state. Runtime network domains may be approved and executed, but domain-level egress filtering is not implemented yet; the UI must disclose this limitation.

### Test Before Install

Automation and hybrid skills must include tests. Proposed executable skills cannot be installed unless their manifest validates, permissions are understood, tests pass, and required approvals are satisfied.

### Version Safety

Never modify the active installed skill version in place. Updates must copy the active version into a new draft/proposed version folder. Activation only switches pointers after validation/tests pass and changed permissions are approved. Keep at most three non-discarded versions per skill.

### Per-Skill Operation Safety

The backend must prevent overlapping per-skill operations:

- one run per skill at a time,
- one install/update/repair/delete operation per skill at a time,
- no run while the same skill is installing, updating, repairing, or deleting,
- no delete while a run is active.

Do not add a global queue that blocks unrelated skills or normal chat.

## Agent System Summary

The bounded agent workflow uses these roles:

- ProductManagerAgent
- BuilderAgent
- TesterAgent

Permission review is deterministic backend logic, not an agent. There is no separate planner, reviewer, repairer, permission analyst, or security reviewer agent in the MVP.

Agent behavior is defined by instruction files in `backend/app/agent_instructions/`. Backend services choose the instruction file for each bounded action, call Codex through `CodexService`, validate structured outputs, and persist workflow artifacts in the database and `runtime/agent_runs/`.

Agents must not install skills, run skills, approve permissions, install packages silently, edit app source while building an application skill, or bypass backend safety checks.

## Implementation Rules

- Keep changes small, reviewable, and testable.
- Follow existing backend/frontend patterns before introducing new abstractions.
- Add or update tests when behavior changes.
- Keep generated-skill code isolated from application code.
- Keep UI state informative, but enforce safety in the backend.
- Do not remove or rewrite user/generated skill files unless the task explicitly requires it.
- If changing workflow behavior, update the relevant file under `docs/` and keep this `AGENTS.md` high-level.

## Verification

Common checks:

```powershell
cd backend
..\.venv\Scripts\python.exe -m pytest

cd ..\frontend
npm run build
```

The frontend build may need normal filesystem access for Vite/esbuild config loading in the Codex sandbox. If the sandbox denies config reads, rerun the same `npm run build` command with the narrow build escalation.
