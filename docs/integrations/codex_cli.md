# Codex CLI Integration

The backend can use the local Codex CLI for direct chat, Project-mode plausibility review, skill planning, Builder edits, Tester test writing, repairs, updates, and ProductManager summaries.

The backend also starts one persistent local `codex app-server --stdio` child process for account allowance reads. It uses `account/rateLimits/read` to expose the current 5-hour and weekly windows. App Server availability failures are reported by the usage API and do not block normal chat or skill runtime.

DAG builds keep a 5% reserve in both account windows. A new ready-node batch is not admitted when either window has less than 5% remaining; already admitted parallel-ready work is allowed to reach its batch boundary before the run pauses.

Project-build Codex CLI invocations use JSONL output plus the final-message file. The backend persists the `turn.completed` token breakdown for ProductManager, Builder, and Tester invocations. Records include adapter and model identity so another adapter can supply the same usage contract later. Direct chat and installed skill runtime calls are not added to project-build token totals.

## Defaults

```text
PERSONAL_AGENT_CODEX_MODE=auto
PERSONAL_AGENT_CODEX_COMMAND=codex
PERSONAL_AGENT_CODEX_SANDBOX=workspace-write
PERSONAL_AGENT_CODEX_PLAUSIBILITY_SANDBOX=read-only
PERSONAL_AGENT_CODEX_CHAT_SANDBOX=read-only
PERSONAL_AGENT_CODEX_SKILL_SANDBOX=read-only
PERSONAL_AGENT_CODEX_APPROVAL_POLICY=never
PERSONAL_AGENT_CODEX_ENABLE_SEARCH=auto
```

`auto` uses real Codex when `codex` is on PATH, otherwise fake/dev adapters are used.

## Setup

```powershell
codex login
codex doctor
```

Optional overrides:

```powershell
$env:PERSONAL_AGENT_CODEX_MODE = "real"
$env:PERSONAL_AGENT_CODEX_MODEL = "gpt-5"
$env:PERSONAL_AGENT_CODEX_TIMEOUT_SECONDS = "300"
```

## Sandbox Modes

- Chat and plausibility: read-only project root.
- ProductManager workflow actions: read-only `runtime/product_manager` workspace. ProductManager returns structured JSON on stdout; the backend parses it and writes workflow artifacts such as `blueprint.json`, `permissions.json`, and `task_dag.json`.
- Builder/Tester skill generation, build, repair, and update: `workspace-write` scoped to the controlled skill or draft-version directory passed with `-C`.
- Backend-mediated skill Codex calls: read-only runtime workspace.

Codex must not modify backend/frontend app source when generating application skills.

## Skill Runtime Calls

Skills must not shell out to the Codex CLI. Installed enabled executable skills may call the backend Skill Codex Call API:

```text
POST /skills/{skill_id}/codex
```

The runner sets `PERSONAL_AGENT_SKILL_ID` and `PERSONAL_AGENT_BACKEND_URL` for generated skill code. The backend validates runtime approval and manifest `permissions.codex` before invoking Codex. `codex_permissions.internet_access=true` is accepted only when runtime `network` entries were approved for the skill.

## Web Search

Build-time Codex search may be enabled when the approved plan requests runtime `network` entries or package dependencies. This is for generation research only. It does not grant runtime access to generated skills.

## Prohibited CLI Options

Do not use:

```text
--dangerously-bypass-approvals-and-sandbox
--sandbox danger-full-access
```
