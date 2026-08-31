# Backend API and Services

The backend is a FastAPI app in `backend/app/main.py`. Routers live under `backend/app/routers/`; services live under `backend/app/services/`.

## Main Routers

- `/chat`: normal chat and project-mode entry point, plus conversation history cleanup by frontend conversation id.
- `/act/sessions`: durable Act session list/create/read/archive, asynchronous turn enqueue, and queued/running turn cancellation.
- `/memory-facts`: explicit user memory CRUD.
- `/skills`: skill listing/detail, function/web-app enable/disable, proposed skill workflow, runs, validation, install/reject/delete, files, versions, repair, runtime permissions, and backend-mediated skill Codex calls. Bare skill-record creation and schedule creation are not exposed; user-facing creation must use the controlled proposed-skill workflow.
- `/functions/catalog`: the unified backend-core, user, and integration function catalog with availability state and reasons for ProductManager and the Functions UI.
- `/functions`: installed user-function discovery and ephemeral run-capability-authenticated invocation.
- `/web-apps`: lazy application sessions, instance diagnostics, bounded audit records, explicit stop, and instance-capability-authenticated Codex calls. The host-routed proxy is internal and omitted from OpenAPI.
- `/settings/integrations/github`: trusted add/replace/status/remove management for the single write-only GitHub credential.
- `/settings/integrations/atlas`: sanitized Atlas lifecycle, directory, optional owned-process automatic-unlock passphrase, restart, and unlock controls.
- `/settings/integrations/notion`: trusted status/add/replace/remove management for the write-only private-connection token. `/settings/integrations/notion/data-sources` separately saves or clears the Todo and Reports IDs together; save validates both exact schemas with the stored token before atomically replacing either ID.
- `/settings/integrations/google`: sanitized shared Google OAuth-client status plus write-only configure/remove routes. One client is shared by Calendar and Gmail; removal or replacement requires both service grants to be disconnected.
- `/settings/integrations/google-calendar`: sanitized Calendar grant status and local disconnect. Its bodyless OAuth start and hidden callback use the shared client but Calendar-specific state, scopes, refresh token, and account identity.
- `/settings/integrations/gmail`: sanitized Gmail grant status and local disconnect. Its bodyless OAuth start and hidden callback use the same shared client but Gmail-specific state, scopes, refresh token, and independently selected account.
- `/settings/integrations/telegram` and `/settings/integrations/telegram-agent`: independent sanitized notification/approval and Act bot status, disconnect, and pairing routes. Pairing start accepts a write-only token and returns a one-time private-chat code.
- hidden integration capability routes: exact function-run and web-app-instance relay endpoints; no operation-specific public proxy or discovery route.
- `/schedules`: generated-service schedule list/detail/edit/pause/resume/run-now plus the read-only platform-service projection.
- `/permission-requests`: permission request list/detail/approve/deny. Approving a build-time generation request continues its linked pending agent run automatically.
- `/invocation-approvals`: separate per-call list/detail/local approve/local deny. It never mutates or reuses permission approvals.
- `/skill-generation-requests`: generation request list/detail/approve/deny/agent-run plus conversation-scoped state recovery for Project chat.
- `/agent-runs`: list/detail/steps/cancel/delete/resume/retry.
- `/usage/codex`: live 5-hour and weekly Codex account allowance from the persistent local App Server.
- `/usage/codex/cli`: effective Codex CLI executable, version, source, candidates, and compatibility status.
- `/settings/codex-models`: live account-aware model catalog and supported reasoning efforts from Codex App Server.
- `/settings/codex-routing`: read or replace the validated single-user invocation routing settings and optional Project build workflow override.
- `/settings/codex-mcp`: inspect, install/repair, or remove the fingerprint-owned host-level Codex MCP registration.
- `/settings/permission-policy`: read the current checked-in permission policy, including default-allowed, approval-required, blocked, and web-application classifications.

`McpFunctionService` snapshots available eligible catalog tools, produces stable typed MCP metadata, rechecks enabled state/availability/contract identity for every call, and routes to the existing trusted function registry or direct-user integration path. `CodexMcpSettingsService` performs structure-preserving atomic host-config registration with ownership fingerprints and rollback.

## Service Responsibilities

### ChatOrchestrator

Coordinates chat requests. Chat mode returns direct answers. Project mode creates generation requests, writes the initial message unchanged through the intent placeholder, and starts build-time approval planning only after ProductManager decides the request is ready to blueprint. If ProductManager asks for clarification, the next Project-mode chat reply is appended to the same generation request. Backend keyword heuristics must not silently create or block skills.

### Act services

`ActSessionService` owns durable sessions, queue admission, cancellation, and archive guards. `ActTurnDispatcher` recovers interrupted work at startup, claims queued turns, resumes saved App Server threads, records live turn ids, and persists terminal output. A thread started in the current App Server process is used directly for its first turn because no rollout exists yet. If a later resume reports a missing rollout before the new turn starts, the dispatcher creates one replacement thread and supplies completed user/assistant transcript as inert historical context; it never replays prior tool calls or a turn that already received a live Codex turn id. `ActAppServerService` owns a dedicated App Server process, enforces the current writable workspace root and instructions on every resume, reloads the Eidolon MCP registration, and verifies that tools are present before execution. Running turns are never replayed after a restart; queued turns have not crossed the side-effect boundary and remain eligible.

### ProductManager planning

There is no pre-ProductManager skill-plan service. Project mode creates a minimal pending generation request, then ProductManager refines intent, performs plausibility review, and writes the blueprint and permission plan. Plausibility receives only the `blocked` section of `backend/app/static/default_permissions.json`; blueprint and update planning receive its complete `default_allowed`/`requires_approval`/`blocked` policy. The build blueprint owns one safe name, one description, runtime, complete function/service input/output JSON Schemas, expected user-visible behavior, selected function ids, and service-only schedule intent. The backend derives integration providers and operations from function ids; provider-specific resource authorization comes from the generated manifest. Fake Codex adapters support deterministic tests and local development.

### AgentWorkflowService

Coordinates the common bounded lifecycle for build, repair, and update. For new builds, it owns the deterministic intent passthrough placeholder, ProductManager plausibility review, blueprint and permission decisions, deterministic build-time approval, immediate backend dependency provisioning, and the workflow-neutral agent-free final validator. It stores the effective top-level `build_workflow` string separately from `blueprint.json`, then dispatches post-approval execution through the project-build workflow registry. When a single-user workflow override is configured, it replaces ProductManager's choice before validation and persistence.

Workflow persistence and DAG reasoning are separate collaborators. `AgentRunArtifactStore` owns the controlled `runtime/agent_runs/run_<id>` tree, JSON/text artifact I/O, task status files, and validated interface-artifact movement. `TaskDagService` owns node normalization, structural and file-claim validation, topological ordering, dependency-path checks, and ready-batch calculation. The orchestrator and workflow executors call those narrow interfaces directly; there is no compatibility forwarding layer on `AgentWorkflowService`.

### Project Build Workflows

Trusted workflow modules live under `backend/app/workflows/`. Each package owns its executor, Markdown instructions, and prompt composition. The common preflight uses a backend intent passthrough placeholder, while `common` owns ProductManager plausibility review, blueprint generation, permission planning, and workflow selection before dispatch. `task_dag` owns task-DAG planning prompts, Builder/Tester/repair prompts, execution, final end-to-end test authoring, and DAG resume/retry behavior while delegating structural validation and ready-batch calculation to `TaskDagService`. `single_codex` owns the prompt and executor for one Codex planning, build, and test-authoring invocation; any invocation or validation error is terminal for that run, and its resume/retry endpoints cannot invoke Builder again. A separate new single-Codex build atomically stages an obsolete proposed folder before creating a clean workspace, so sandbox-owned cache ACLs do not block replacement. Both workflows use the same deterministic backend scan/manifest/package/test validator. Unknown workflow names are rejected by the registry.

`FunctionCatalogService` persists one catalog at `runtime/function_catalog.json`, seeded with checked-in backend-core definitions and refreshed from the typed integration registry plus installed user-function contracts. ProductManager receives only currently available agent-selectable entries as id/category/title/description/risk; Act-only MCP tools remain outside Project generation. The blueprint selects function ids. A Task DAG assigns those approved ids to nodes through `function_ids`; the backend resolves full schemas and invocation guidance into that node's `function_context`. The single-Codex workflow receives full context for every blueprint-selected function. Disabled user skills, invalid or stale function contracts, missing runtime approval, disconnected integrations, and Act-only tools remain visible in the UI but are not offered to ProductManager.

`PlatformServiceDispatcher` owns endpoint-only trusted backend dispatch. The daily Notion Done cleanup is not in the function catalog and cannot be selected by ProductManager, another skill, or MCP. `SchedulerService` alone invokes it through the trusted direct-user Notion provider path. It is registered as a fixed platform APScheduler job rather than a `SkillSchedule` row, while `/schedules` exposes a read-only projection for visibility.

The project-build workflow registry is backend-managed and contains only trusted checked-in executors. ProductManager selects a registered name; it cannot supply executable workflow code.

### CodexService

Acts as the Codex application facade for shared real/fake adapter integration, structured-output parsing, model routing, controlled workspaces, manifest helpers, and invocation safety. `ProductManagerContractService` separately normalizes untrusted ProductManager blueprint, permission, review, and task-DAG JSON into backend-owned allowlisted contracts; permission output is restricted to the config-derived approval template. Project-build prompt composition is supplied by the owning workflow package. Update and standalone repair prompt composition remains role-based until those workflows are modularized. ProductManager Codex calls are forced to `read-only`; writable workflow/Builder/Tester calls are forced to `workspace-write` and scoped to controlled skill or draft-version directories.

`CodexInvocationRecorder` normalizes per-invocation token metadata into an adapter-neutral record and separately buffers the exact composed prompt and exact final adapter response until the active workflow step consumes them. `AgentWorkflowService` attaches both records to the active agent step and maintains agent-run totals. Backend-only steps never consume or expose an agent transcript. Successful runtime calls update separate token totals; failed calls retain the resolved CLI path/version/source, exit code, error type, concise error detail, and a bounded stderr tail while leaving token totals at zero when no completed turn exists.

### CodexUsageService

Owns one persistent `codex app-server --stdio` child process for the FastAPI lifespan, performs JSON-RPC initialization, reads `account/rateLimits/read`, and normalizes the primary 300-minute and secondary 10,080-minute windows. Workflow pause checks are fail-open when allowance data is unavailable and pause only when Codex reports exhaustion.

### CodexCliService

Discovers Codex Desktop and PATH executables, probes their semantic versions, honors `PERSONAL_AGENT_CODEX_COMMAND` as a strict explicit override, selects the newest compatible automatic candidate, and supplies one resolved executable to every Codex-backed service. It exposes an optional minimum-version compatibility gate without owning future model or reasoning-effort selection policy.

### CodexRoutingService

Persists and validates unified invocation choices against the live App Server model catalog. Resolution precedence is invocation override, action or Builder workflow/difficulty setting, role default, legacy environment default, then the catalog default. The single-Codex build action has its own Builder route; Task-DAG Builder calls continue to route by backend-validated node difficulty. The service returns requested and effective values plus the route source. The current provider is `codex_cli`; the contract keeps provider identity explicit so a future adapter can participate without being implemented here.

### PermissionService

Deterministically reviews permissions, dependencies, and declared function relationships. It creates approval requests, refreshes stale pending requests, approves/denies requests, detects permission expansion, checks install/run eligibility, and syncs waiting agent steps. Build-time and runtime reviews show every declared target's risk and availability, which low-risk relationships need no extra approval, and which medium/high-risk relationships have separate caller-target requests. The permission router resumes a linked generation run after a build-time approval regardless of whether the decision came from Chat or the global Approval Requests page. Runtime `permissions.codex.call_response` requires explicit approval and defaults to disabled when omitted or false. Runtime `permissions.codex.internet_access` is supported only when the skill also has approved runtime `network` entries. Other Codex permission fields are blocked in this milestone.

### FunctionRegistryService

Owns dynamic function discovery, caller requirement review, caller-target approval fingerprints, ephemeral run-capability resolution, JSON Schema input/output validation, availability checks, and invocation attribution. Manual function runs, function callers, scheduled service callers, backend callers, and web applications use this service to invoke declared function targets. Services never appear as targets. It trusts neither caller-supplied ids nor target paths/commands/permissions/risk. The public registry contract omits internal paths, commands, credentials, and container details. Nested function-to-function calls are rejected and deferred to `TODO-013`.

### IntegrationService and Provider Adapters

`IntegrationService` owns provider-specific skill-contract fingerprints, separate integration approvals, caller/version/manifest/scope/schema enforcement, last-moment secret retrieval, and sanitized audits. `integration_registry.py` is the single typed operation authority. Separate GitHub, Atlas, Notion, Google Calendar, Gmail, and Telegram adapters construct fixed requests and normalize responses. Calendar and Gmail share one OS-stored Google OAuth application client while retaining separate grants, scopes, refresh tokens, account identities, and authorizations. Gmail exposes the four provider-neutral `email.*` operations; `email.send` enters the separate invocation-approval control plane before any Gmail credential retrieval. Telegram owns bounded notifications while approval delivery remains backend control-plane behavior. Google Calendar exposes exactly five primary-calendar event operations and never persists access tokens or event content. `TodoService` and `ReportService` depend on narrow providers; `NotionTodoProvider` and `NotionReportProvider` share bounded transport behavior while fixing separate configured sources as their complete resource boundaries. The Reports provider supports exact metadata plus raw top-level block pagination and raw create children. The Atlas adapter derives bounded projections, Goal progression, filters, and Knowledge navigation from Atlas's native APIs without authorization headers; `Know_node` performs one bounded internal Codex expansion followed by primitive writes. The Atlas lifecycle/settings services own safe startup, attachment, directory changes, legacy credential cleanup, and opt-in owned-process automatic unlock. `SecretStore` has no unsafe fallback. See [GitHub integration](../integrations/github.md), [Atlas integration](../integrations/atlas.md), [Notion integration](../integrations/notion.md), [Google Calendar integration](../integrations/google_calendar.md), [Gmail integration](../integrations/gmail.md), and [Telegram integration](../integrations/telegram.md).

### InvocationApprovalService

Owns bounded per-call submission, backend-selected and persisted presentation presets, Telegram delivery and terminal message edits, local/Telegram decisions, atomic execution claims, contract/account revalidation, deferred dispatch, and restart recovery. The caller supplies the required reason. The service returns a pending receipt immediately and never resumes the original caller. See [Invocation approvals](../security/invocation_approvals.md).

### StaticCapabilityScanner

Performs the deterministic first stage of shared final validation for both Project build workflows. After the backend parses and package-checks the actual generated manifest and confirms its runtime and name match the approved controlled record, it scans bounded Python implementation files for selected direct network, process, browser, filesystem, sensitive-environment, deletion, integration-bypass, and parseability evidence. It also scans HTML/CSS/JavaScript assets for literal absolute browser URLs and rejects web applications that put writable cache beneath their read-only package path. Recognized evidence is compared with explicit manifest declarations and persisted; it never changes permissions. Blocking findings prevent backend test execution and runtime permission review. The single-Codex workflow blocks; the DAG workflow may ask Builder to repair before a rescan. The scanner is intentionally heuristic and does not replace manifest review, CSP, or runtime containment.

### ProposedSkillService

`BuildDependencyService` provisions approved runtime and build-only Python dependencies atomically before workflow dispatch, verifies installed distributions and platform test tooling, and reuses an already verified dependency environment. The shared dependency-environment helper gives Codex and authoritative tests the same interpreter path and `PYTHONPATH`.

`ProposedSkillService` validates proposed skill packages and their already-provisioned dependency contract, reads bounded text package files including web assets without descending into inaccessible transient directories, installs proposed skills, rejects or deletes proposed skills, and creates one paused schedule for an installed service from its required manifest intent. Validation and install never invoke `pip`. Installation excludes build-only dependencies plus sandbox/Codex/test bookkeeping, recovers interrupted copies idempotently, and moves source cleanup outside active roots before treating it as best-effort. Deletion atomically stages both proposed and installed folders before database cleanup, so Windows ACL or lock failures cannot leave contradictory active lifecycle state. It does not expose a sample-skill or bare-record creation path.

### SkillRunner

Selects Docker or local/dev runner for bounded `function` and `service` skills. Runners validate manifests, enforce supported permissions, run tests, execute entrypoints with JSON input, require JSON stdout, capture logs, and store `skill_runs`. Both runners defensively reject `web_app`; `ServiceRuntimeService` additionally enforces schedule attribution and service input/output schemas. Common nonzero exit codes produce specific error summaries for command failures, interruption, termination, forced kills, and segmentation faults while raw process diagnostics remain in stderr. A valid JSON process exit is not automatically treated as success: top-level `status: "partial"` and `status: "failed"` outputs become matching backend run statuses with a concise failure summary, and any recorded failed runtime Codex call prevents a succeeded run status. Runtime Codex usage is associated with the currently running row under the existing per-skill operation lock. Runners provide `PERSONAL_AGENT_SKILL_ID` and `PERSONAL_AGENT_BACKEND_URL`; authorized direct/backend/scheduled callers also receive one ephemeral bounded-run capability for their entrypoint only. Tests never receive that token. No-internet Docker callers use a transient internal-network relay that forwards only declared Function registry discovery/invocation, Codex, and integration invocation paths.

### WebAppRuntimeService

Owns persistent `web_app` preconditions, version-pinned instance startup/reuse, readiness, distinct session origins, hashed instance/session capabilities, idle/stale/shutdown cleanup, bounded logs/audit records, and containment-policy reporting. `PersistentWebAppLauncher` uses the trusted runner image and ASGI host in Docker by default. No-network applications remain on an internal network behind a trusted ingress and exact-capability relay; explicit local/development mode is labeled less isolated. The service does not create an indefinite `SkillRun` or hold an operation lock for idle lifetime. The gateway rechecks installed/enabled/runtime/active-version state on every request. Its scoped server helper supports both Codex and registry-controlled integration calls without exposing instance capability material to browser code. Full behavior is in [Sandboxed web applications](../runtime/web_applications.md).

## Skill Codex API

Sandboxed installed function skills use:

```text
function_runtime_capabilities.call_codex(...)
  -> POST /functions/capabilities/codex
```

Request body:

```json
{
  "prompt": "string",
  "context": {},
  "model": "gpt-5",
  "codex_permissions": {
    "call_response": true,
    "internet_access": false
  }
}
```

The hidden route requires the current function run's ephemeral bearer token. The backend derives the caller skill and version from that token, then checks installed/enabled status, `runtime = function`, runtime approval, manifest validity, `permissions.codex.call_response`, `permissions.codex.internet_access`, and whether requested Codex internet access is backed by approved runtime `network` entries. Trusted local compatibility callers may still use `POST /skills/{skill_id}/codex`; sandboxed code must not use that caller-supplied skill-id route. Web applications use the instance-scoped trusted helper and `/web-apps/capabilities/codex`. Shell access remains prohibited; skills must not call the Codex CLI directly.

Current backend-core function catalog entries:

- `backend.codex.call`: bounded Skill Codex call.

New backend-core functions are not PM-visible until their catalog contracts and permissions are designed.

### SkillVersionService

Creates version records, copies active versions into draft folders, validates versions, compares all bounded text package files including web assets, creates runtime approval requests when permissions change, activates versions, and discards drafts. Runtime conversion is rejected. Service activation preserves the existing schedule and rejects a candidate whose input schema is incompatible with stored schedule input. Web-app activation stops running instances before moving the active pointer; draft update/repair leaves the active application untouched.

### SchedulerService

Uses APScheduler to manage exactly one schedule per generated service and the fixed backend-owned Notion cleanup service. Functions and web applications cannot declare or receive schedules. Manifest-declared service intent is registered as a paused row during install.
Schedule-mutating API routes use the FastAPI lifespan's shared APScheduler instance with their request-scoped database session, so edit, pause, and resume update the live scheduler immediately. Run Now works for active or paused generated-service schedules without changing activation state. Automatic execution remains active-only and every path rechecks runtime permissions, integrations, schema compatibility, and the per-skill operation guard.

Startup reconciliation computes only the latest due occurrence for each active schedule. Normal callbacks and startup catch-ups atomically claim the same deterministic occurrence key before execution. Any existing claim suppresses another invocation, including after failure or interruption. Runtime state preserves activation boundaries and interval anchors; platform schedules use the same durable occurrence ledger as generated services.

### SkillOperationGuard

Implements local per-skill operation locks. It prevents overlapping operations on the same skill without blocking unrelated skills or chat.

## Backend Safety Rule

Frontend state is never the safety boundary. The backend must enforce status checks, permission checks, runner support, operation locks, and filesystem path boundaries.
