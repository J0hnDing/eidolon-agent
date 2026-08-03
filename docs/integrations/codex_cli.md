# Codex CLI Integration

The backend can use the local Codex CLI for direct chat, Project-mode plausibility review, skill planning, Builder edits, Tester test writing, repairs, updates, and ProductManager summaries.

The backend also starts one persistent local `codex app-server --stdio` child process for account allowance reads. It uses `account/rateLimits/read` to expose the current 5-hour and weekly windows. App Server availability failures are reported by the usage API and do not block normal chat or skill runtime.

DAG builds keep a 5% reserve in both account windows. A new ready-node batch is not admitted when either window has less than 5% remaining; already admitted parallel-ready work is allowed to reach its batch boundary before the run pauses.

Project-build Codex CLI invocations use JSONL output plus the final-message file. ProductManager actions also pass an action-specific JSON Schema through `--output-schema`; blueprint responses constrain their known build, repair, or update fields while leaving the nested callable input/output JSON Schema documents open for user-defined properties. Their existing prompt contracts and backend parsing, sanitization, catalog checks, and fallbacks remain defense in depth. Builder, Tester, and single-Codex build calls do not use a final-response schema because their authoritative outputs are controlled files and validation results. The backend persists the `turn.completed` token breakdown for ProductManager, Builder, and Tester invocations. Records include adapter, role, route source, requested/effective model, requested/effective reasoning effort, and Builder difficulty when applicable. Installed skill runtime calls use the same normalized invocation shape but are stored on the active skill run; they and direct chat are not added to project-build token totals.

## Defaults

```text
PERSONAL_AGENT_CODEX_MODE=auto
PERSONAL_AGENT_CODEX_SANDBOX=workspace-write
PERSONAL_AGENT_CODEX_PLAUSIBILITY_SANDBOX=read-only
PERSONAL_AGENT_CODEX_CHAT_SANDBOX=read-only
PERSONAL_AGENT_CODEX_SKILL_SANDBOX=read-only
PERSONAL_AGENT_CODEX_APPROVAL_POLICY=never
PERSONAL_AGENT_CODEX_ENABLE_SEARCH=auto
```

`auto` uses real Codex when a compatible executable is discovered, otherwise fake/dev adapters are used.

## Executable Resolution and Compatibility

Every backend Codex path, including Chat, ProductManager, Builder, Tester, planning, plausibility review, skill runtime calls, and the persistent App Server, uses one central CLI resolver.

When `PERSONAL_AGENT_CODEX_COMMAND` is set, that executable is an explicit override and no automatic fallback replaces it. Without an override, the backend checks the Codex Desktop installation and every `codex` executable visible on `PATH`, runs `<candidate> --version`, and selects the newest valid semantic version. Codex Desktop wins a version tie. This prevents an older PATH installation from silently taking precedence over a newer Desktop-bundled CLI.

The Settings page displays the effective executable, version, source, and compatibility status. `GET /usage/codex/cli` exposes the same adapter-neutral status contract for future model and reasoning-effort capability checks.

Callers may also supply an operation-specific minimum version when requesting the resolved command. This is unused by current model selection, but gives a future model/effort capability policy a preflight hook without moving executable discovery into that policy.

## Model and Reasoning-Effort Routing

The backend reads the account-aware model picker through App Server `model/list`. Only visible advertised models and their advertised `supportedReasoningEfforts` may be saved. A configured combination is revalidated before a real invocation; unavailable or unsupported combinations block with an actionable error instead of silently selecting another model.

Routing settings cover:

- normal Chat independently;
- ProductManager refine-intent, plausibility, blueprint/permissions, task-DAG, repair, and update actions;
- Builder single-Codex builds, `easy`, `medium`, and `hard` DAG nodes, plus repair and update actions;
- Tester task, final end-to-end, and update actions.

The single-Codex workflow uses the explicit Builder `single_codex` route. Task-DAG Builder routing reads the backend-validated `difficulty` already present on each task node. The task DAG contains no model or reasoning-effort fields, so ProductManager cannot invent or select model ids.

Precedence is:

```text
explicit invocation override
action or Builder workflow/difficulty setting
role default
legacy PERSONAL_AGENT_CODEX_MODEL / PERSONAL_AGENT_CODEX_REASONING_EFFORT
Codex catalog default
```

The CLI receives the effective model through `--model` and effective effort through `--config model_reasoning_effort=...`.

An optional minimum version can be enforced:

```powershell
$env:PERSONAL_AGENT_CODEX_MIN_VERSION = "0.140.0"
```

If the explicit override cannot be version-checked, or the selected CLI is older than the configured minimum, real Codex operations fail before starting with an actionable compatibility error. Refreshing the Settings page re-runs discovery and version checks.

## Setup

```powershell
codex login
codex doctor
```

Optional overrides:

```powershell
$env:PERSONAL_AGENT_CODEX_MODE = "real"
$env:PERSONAL_AGENT_CODEX_COMMAND = "C:\path\to\codex.exe"
$env:PERSONAL_AGENT_CODEX_MODEL = "gpt-5"
```

## Hard Timeouts

Project-build hard timeouts are backend-owned and action-specific. Each limit applies to one Codex CLI invocation,
not to the complete workflow. Task-DAG nodes and retries therefore receive separate invocation limits, while the
single-Codex workflow uses one larger limit for its combined planning, implementation, and focused testing call.

| Codex action | Hard timeout |
| --- | ---: |
| ProductManager intent refinement and plausibility review | 120 seconds |
| ProductManager blueprint/permissions, task-DAG planning, repair planning, and update review | 180 seconds |
| Builder task, update, and repair | 600 seconds |
| Tester task, final E2E authoring, and update testing | 300 seconds |
| Single-Codex combined build | 900 seconds |
| Installed-skill bounded Codex call | 45 seconds |

Unknown legacy actions use a 300-second compatibility fallback. Direct chat, standalone plausibility review, and
standalone skill planning retain their separate service-specific 120-second limits. Progress-aware idle timeouts,
whole-workflow budgets, and partial recovery are not part of these hard limits and remain tracked in `TODO-012`.

## Sandbox Modes

- Chat: read-only project root.
- Plausibility: read-only project root with a final-response JSON Schema for the machine-consumed decision.
- ProductManager workflow actions: read-only `runtime/product_manager` workspace. ProductManager returns CLI-schema-constrained JSON on stdout; the backend parses and sanitizes it, then writes workflow artifacts such as `blueprint.json`, `permissions.json`, and `task_dag.json`.
- Builder/Tester skill generation, build, repair, and update: `workspace-write` scoped to the controlled skill or draft-version directory passed with `-C`.
- Backend-mediated skill Codex calls: read-only runtime workspace with a final-response JSON Schema for the bounded response envelope.

Codex must not modify backend/frontend app source when generating application skills.

## Skill Runtime Calls

Skills must not shell out to the Codex CLI. Installed enabled function skills may call the backend Skill Codex Call API:

```text
POST /skills/{skill_id}/codex
```

The runner sets `PERSONAL_AGENT_SKILL_ID` and `PERSONAL_AGENT_BACKEND_URL` for generated skill code. The backend validates runtime approval and manifest `permissions.codex` before invoking Codex. `codex_permissions.internet_access=true` is accepted only when runtime `network` entries were approved for the skill.

Web applications do not use the caller-supplied skill-id route. Their server process uses `web_runtime_capabilities.call_codex`, which authenticates `/web-apps/capabilities/codex` with a scoped instance token and rechecks the enabled active version plus manifest declarations. Browser code never receives the token.

During an active skill run, the backend appends a success or failure invocation record to that `skill_runs` row. Success records include adapter/model identity, the invoked CLI path when applicable, and token breakdowns and update the runtime aggregate counters. Failure records include the resolved CLI path/version/source, exit code, error type, concise error detail, and a bounded stderr tail; token counters remain zero when no completed turn was emitted. Per-skill operation locking makes the active row unambiguous. Calls made outside an active run are not attributed to run history.

For a failed runtime call, inspect the run's `error_message` and `codex_invocations_json` through Skill Run History or `GET /skills/{skill_id}/runs`. Compare its recorded `cli_version` and `cli_path` with `GET /usage/codex/cli` or the Codex Settings page. This preserves the historical executable identity even if Codex Desktop updates before the failure is investigated.

## Web Search

Build-time Codex search may be enabled when the approved plan requests runtime `network` entries or package dependencies. This is for generation research only. It does not grant runtime access to generated skills.

## Prohibited CLI Options

Do not use:

```text
--dangerously-bypass-approvals-and-sandbox
--sandbox danger-full-access
```
