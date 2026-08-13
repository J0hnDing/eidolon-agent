# Backend API and Services

The backend is a FastAPI app in `backend/app/main.py`. Routers live under `backend/app/routers/`; services live under `backend/app/services/`.

## Main Routers

- `/chat`: normal chat and project-mode entry point, plus conversation history cleanup by frontend conversation id.
- `/memory-facts`: explicit user memory CRUD.
- `/skills`: skill listing/detail, installed-skill enable/disable, proposed skill workflow, runs, validation, install/reject/delete, files, versions, schedules, repair, runtime permissions, and backend-mediated skill Codex calls. Bare skill-record creation is not exposed; user-facing creation must use the controlled proposed-skill workflow.
- `/functions/catalog`: the unified backend-core, user, and integration function catalog with availability state and reasons for ProductManager and the Functions UI.
- `/functions`: installed user-function discovery and ephemeral run-capability-authenticated invocation.
- `/web-apps`: lazy application sessions, instance diagnostics, bounded audit records, explicit stop, and instance-capability-authenticated Codex calls. The host-routed proxy is internal and omitted from OpenAPI.
- `/settings/integrations/github`: trusted add/replace/status/remove management for the single write-only GitHub credential.
- `/settings/integrations/atlas`: sanitized Atlas lifecycle, directory, key, automatic-unlock passphrase, restart, and unlock controls.
- hidden integration capability routes: exact function-run and web-app-instance relay endpoints; no operation-specific public proxy or discovery route.
- `/schedules`: schedule list/detail/approve/deny/pause/resume/delete/run-now.
- `/permission-requests`: permission request list/detail/approve/deny. Approving a build-time generation request continues its linked pending agent run automatically.
- `/skill-generation-requests`: generation request list/detail/approve/deny/agent-run plus conversation-scoped state recovery for Project chat.
- `/agent-runs`: list/detail/steps/cancel/delete/resume/retry.
- `/usage/codex`: live 5-hour and weekly Codex account allowance from the persistent local App Server.
- `/usage/codex/cli`: effective Codex CLI executable, version, source, candidates, and compatibility status.
- `/settings/codex-models`: live account-aware model catalog and supported reasoning efforts from Codex App Server.
- `/settings/codex-routing`: read or replace the validated single-user invocation routing settings and optional Project build workflow override.
- `/settings/permission-policy`: read the current checked-in permission policy, including default-allowed, approval-required, blocked, and web-application classifications.

## Service Responsibilities

### ChatOrchestrator

Coordinates chat requests. Chat mode returns direct answers. Project mode creates generation requests, sends them through ProductManager intent refinement and plausibility review, and starts build-time approval planning only after ProductManager decides the request is ready to blueprint. If ProductManager asks for clarification, the next Project-mode chat reply is appended to the same generation request. Backend keyword heuristics must not silently create or block skills.

### ProductManager planning

There is no pre-ProductManager skill-plan service. Project mode creates a minimal pending generation request, then ProductManager refines intent, performs plausibility review, and writes the blueprint and permission plan. Plausibility receives only the `blocked` section of `backend/app/static/default_permissions.json`; blueprint and update planning receive its complete `default_allowed`/`requires_approval`/`blocked` policy. The blueprint owns the safe skill identity, runtime, complete function input/output JSON Schemas, selected function ids, integration resource scopes, schedule intent, and acceptance criteria. Fake Codex adapters support deterministic tests and local development.

### AgentWorkflowService

Coordinates the common bounded lifecycle for build, repair, and update. For new builds, it owns ProductManager intent refinement, plausibility review, blueprint and permission decisions, deterministic build-time approval, immediate backend dependency provisioning, and the workflow-neutral agent-free final validator. It stores the effective top-level `build_workflow` string separately from `blueprint.json`, then dispatches post-approval execution through the project-build workflow registry. When a single-user workflow override is configured, it replaces ProductManager's choice before validation and persistence.

Workflow persistence and DAG reasoning are separate collaborators. `AgentRunArtifactStore` owns the controlled `runtime/agent_runs/run_<id>` tree, JSON/text artifact I/O, task status files, and validated interface-artifact movement. `TaskDagService` owns node normalization, structural and file-claim validation, topological ordering, dependency-path checks, and ready-batch calculation. The orchestrator and workflow executors call those narrow interfaces directly; there is no compatibility forwarding layer on `AgentWorkflowService`.

### Project Build Workflows

Trusted workflow modules live under `backend/app/workflows/`. Each package owns its executor, Markdown instructions, and prompt composition. `common` owns intent refinement, plausibility review, blueprint generation, permission planning, and workflow selection before dispatch. `task_dag` owns task-DAG planning prompts, Builder/Tester/repair prompts, execution, final end-to-end test authoring, and DAG resume/retry behavior while delegating structural validation and ready-batch calculation to `TaskDagService`. `single_codex` owns the prompt and executor for one Codex planning, build, and test-authoring invocation; any invocation or validation error is terminal for that run, and its resume/retry endpoints cannot invoke Builder again. A separate new single-Codex build atomically stages an obsolete proposed folder before creating a clean workspace, so sandbox-owned cache ACLs do not block replacement. Both workflows use the same deterministic backend scan/manifest/package/test validator. Unknown workflow names are rejected by the registry.

`FunctionCatalogService` persists one catalog at `runtime/function_catalog.json`, seeded with checked-in backend-core definitions and refreshed from the typed integration registry plus installed user-function contracts. ProductManager receives only currently available entries as id/category/title/description/risk. The blueprint selects function ids. A Task DAG assigns those approved ids to nodes through `function_ids`; the backend resolves full schemas and invocation guidance into that node's `function_context`. The single-Codex workflow receives full context for every blueprint-selected function. Disabled user skills, invalid or stale function contracts, missing runtime approval, and disconnected integrations remain visible in the UI but are not offered to ProductManager.

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

Deterministically reviews permissions, dependencies, and declared function relationships. It creates approval requests, refreshes stale pending requests, approves/denies requests, detects permission expansion, checks install/run eligibility, and syncs waiting agent steps. Build-time and runtime reviews show every declared target's risk and availability, which low-risk relationships need no extra approval, and which medium/high-risk relationships have separate caller-target requests. The permission router resumes a linked generation run after a build-time approval regardless of whether the decision came from Chat or the global Approval Requests page. Runtime `permissions.codex.call_response` is granted by default. Runtime `permissions.codex.internet_access` is supported only when the skill also has approved runtime `network` entries. Other Codex permission fields are blocked in this milestone.

### FunctionRegistryService

Owns dynamic function discovery, caller requirement review, caller-target approval fingerprints, ephemeral run-capability resolution, JSON Schema input/output validation, availability checks, and invocation attribution. Manual runs, schedules, function callers, backend callers, and web applications converge on this service before the existing bounded runner. It trusts neither caller-supplied ids nor target paths/commands/permissions/risk. The public registry contract omits internal paths, commands, credentials, and container details. Nested function calls are rejected and deferred to `TODO-013`.

### IntegrationService and Provider Adapters

`IntegrationService` owns provider-specific skill-contract fingerprints, separate integration approvals, caller/version/manifest/scope/schema enforcement, last-moment secret retrieval, and sanitized audits. `integration_registry.py` is the single typed operation authority. Separate GitHub and Atlas adapters construct fixed authenticated requests and normalize responses. The Atlas lifecycle/settings services own safe startup, attachment, directory changes, key management, and opt-in automatic unlock. `Know_node` adds one bounded internal Codex expansion before Atlas's atomic mutation. `SecretStore` uses distinct Windows Credential Manager namespaces with no unsafe fallback. See [GitHub integration](../integrations/github.md) and [Atlas integration](../integrations/atlas.md).

### StaticCapabilityScanner

Performs the deterministic first stage of shared final validation for both Project build workflows. After the backend parses and package-checks the actual generated manifest and confirms its runtime and name match the approved controlled record, it scans bounded Python implementation files for selected direct network, process, browser, filesystem, sensitive-environment, deletion, integration-bypass, and parseability evidence. It also scans HTML/CSS/JavaScript assets for literal absolute browser URLs and rejects web applications that put writable cache beneath their read-only package path. Recognized evidence is compared with explicit manifest declarations and persisted; it never changes permissions. Blocking findings prevent backend test execution and runtime permission review. The single-Codex workflow blocks; the DAG workflow may ask Builder to repair before a rescan. The scanner is intentionally heuristic and does not replace manifest review, CSP, or runtime containment.

### ProposedSkillService

`BuildDependencyService` provisions approved runtime and build-only Python dependencies atomically before workflow dispatch, verifies installed distributions and platform test tooling, and reuses an already verified dependency environment. The shared dependency-environment helper gives Codex and authoritative tests the same interpreter path and `PYTHONPATH`.

`ProposedSkillService` validates proposed skill packages and their already-provisioned dependency contract, reads bounded text package files including web assets without descending into inaccessible transient directories, installs proposed skills, rejects or deletes proposed skills, and registers pending function schedules declared in installed manifests. Validation and install never invoke `pip`. Installation excludes build-only dependencies plus sandbox/Codex/test bookkeeping, recovers interrupted copies idempotently, and moves source cleanup outside active roots before treating it as best-effort. Deletion atomically stages both proposed and installed folders before database cleanup, so Windows ACL or lock failures cannot leave contradictory active lifecycle state. It does not expose a sample-skill or bare-record creation path.

### SkillRunner

Selects Docker or local/dev runner for bounded `function` skills only. Runners validate manifests, enforce supported permissions, run tests, execute entrypoints with JSON input, require JSON stdout, capture logs, and store `skill_runs`. Both runners defensively reject `web_app`. Common nonzero exit codes produce specific error summaries for command failures, interruption, termination, forced kills, and segmentation faults while raw process diagnostics remain in stderr. A valid JSON process exit is not automatically treated as success: top-level `status: "partial"` and `status: "failed"` outputs become matching backend run statuses with a concise failure summary, and any recorded failed runtime Codex call prevents a succeeded run status. Runtime Codex usage is associated with the currently running row under the existing per-skill operation lock. Runners provide `PERSONAL_AGENT_SKILL_ID` and `PERSONAL_AGENT_BACKEND_URL`; authorized direct/backend/scheduled callers also receive one ephemeral Function capability for their entrypoint only. Tests never receive that token. No-internet Docker callers use a transient internal-network relay that forwards only Function registry discovery/invocation and integration invocation paths.

### WebAppRuntimeService

Owns persistent `web_app` preconditions, version-pinned instance startup/reuse, readiness, distinct session origins, hashed instance/session capabilities, idle/stale/shutdown cleanup, bounded logs/audit records, and containment-policy reporting. `PersistentWebAppLauncher` uses the trusted runner image and ASGI host in Docker by default. No-network applications remain on an internal network behind a trusted ingress and exact-capability relay; explicit local/development mode is labeled less isolated. The service does not create an indefinite `SkillRun` or hold an operation lock for idle lifetime. The gateway rechecks installed/enabled/runtime/active-version state on every request. Its scoped server helper supports both Codex and registry-controlled integration calls without exposing instance capability material to browser code. Full behavior is in [Sandboxed web applications](../runtime/web_applications.md).

## Skill Codex API

Installed enabled function skills may call:

```text
POST /skills/{skill_id}/codex
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

Backend checks installed/enabled status, `runtime = function`, runtime approval, manifest validity, `permissions.codex.call_response`, `permissions.codex.internet_access`, and whether requested Codex internet access is backed by approved runtime `network` entries. Web applications cannot use this skill-id route; they use the instance-scoped trusted helper and `/web-apps/capabilities/codex`. Shell access remains prohibited; skills must not call the Codex CLI directly.

Current backend-core function catalog entries:

- `backend.codex.call`: bounded Skill Codex call.

New backend-core functions are not PM-visible until their catalog contracts and permissions are designed.

### SkillVersionService

Creates version records, copies active versions into draft folders, validates versions, compares all bounded text package files including web assets, creates runtime approval requests when permissions change, activates versions, and discards drafts. Activation stops running instances before moving the active pointer; draft update/repair leaves the active application untouched.

### SchedulerService

Uses APScheduler to register approved schedules and trigger scheduled function runs through the same safety path as manual runs. Web applications cannot declare or receive bounded-run schedules. Manifest-declared function schedules are registered as pending records when the proposed skill is installed; approval is still required before activation.
Schedule-mutating API routes use the FastAPI lifespan's shared APScheduler instance with their request-scoped database session, so approval, pause, resume, and deletion update the live scheduler immediately. Run-now is limited to active approved schedules.

### SkillOperationGuard

Implements local per-skill operation locks. It prevents overlapping operations on the same skill without blocking unrelated skills or chat.

## Backend Safety Rule

Frontend state is never the safety boundary. The backend must enforce status checks, permission checks, runner support, operation locks, and filesystem path boundaries.
