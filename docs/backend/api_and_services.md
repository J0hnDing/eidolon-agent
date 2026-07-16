# Backend API and Services

The backend is a FastAPI app in `backend/app/main.py`. Routers live under `backend/app/routers/`; services live under `backend/app/services/`.

## Main Routers

- `/chat`: normal chat and project-mode entry point, plus conversation history cleanup by frontend conversation id.
- `/memory-facts`: explicit user memory CRUD.
- `/skills`: skill listing/detail, installed-skill enable/disable, proposed skill workflow, runs, validation, install/reject/delete, files, versions, schedules, repair, runtime permissions, and backend-mediated skill Codex calls. Bare skill-record creation is not exposed; user-facing creation must use the controlled proposed-skill workflow.
- `/tools`: installed enabled tool skills and tool runs.
- `/schedules`: schedule list/detail/approve/deny/pause/resume/delete/run-now.
- `/permission-requests`: permission request list/detail/approve/deny.
- `/skill-generation-requests`: generation request list/detail/approve/deny/agent-run.
- `/agent-runs`: list/detail/steps/cancel/delete/resume/retry.
- `/usage/codex`: live 5-hour and weekly Codex account allowance from the persistent local App Server.
- `/usage/codex/cli`: effective Codex CLI executable, version, source, candidates, and compatibility status.
- `/settings/codex-models`: live account-aware model catalog and supported reasoning efforts from Codex App Server.
- `/settings/codex-routing`: read or replace the validated single-user invocation routing settings and optional Project build workflow override.

## Service Responsibilities

### ChatOrchestrator

Coordinates chat requests. Chat mode returns direct answers. Project mode creates generation requests, sends them through ProductManager intent refinement and plausibility review, and starts build-time approval planning only after ProductManager decides the request is ready to blueprint. If ProductManager asks for clarification, the next Project-mode chat reply is appended to the same generation request. Backend keyword heuristics must not silently create or block skills.

### SkillPlanService and ProjectPlausibilityService

Use Codex adapters when available to classify project plausibility and generate initial skill plans. Fake adapters support tests and local development.

### AgentWorkflowService

Coordinates the common bounded lifecycle for build, repair, and update. For new builds, it owns ProductManager intent refinement, plausibility review, blueprint and permission decisions, deterministic build-time approval, and the workflow-neutral agent-free final validator. It stores the effective top-level `build_workflow` string separately from `blueprint.json`, then dispatches post-approval execution through the project-build workflow registry. When a single-user workflow override is configured, it replaces ProductManager's choice before validation and persistence.

Workflow persistence and DAG reasoning are separate collaborators. `AgentRunArtifactStore` owns the controlled `runtime/agent_runs/run_<id>` tree, JSON/text artifact I/O, task status files, and validated interface-artifact movement. `TaskDagService` owns node normalization, structural and file-claim validation, topological ordering, dependency-path checks, and ready-batch calculation. The orchestrator and workflow executors call those narrow interfaces directly; there is no compatibility forwarding layer on `AgentWorkflowService`.

### Project Build Workflows

Trusted workflow modules live under `backend/app/workflows/`. Each package owns its executor, Markdown instructions, and prompt composition. `common` owns intent refinement, plausibility review, blueprint generation, permission planning, and workflow selection before dispatch. `task_dag` owns task-DAG planning prompts, Builder/Tester/repair prompts, execution, final end-to-end test authoring, and DAG resume/retry behavior while delegating structural validation and ready-batch calculation to `TaskDagService`. `single_codex` owns the prompt and executor for one Codex planning, build, and test-authoring invocation. Both use the same deterministic backend scan/manifest/package/test validator. Unknown workflow names are rejected by the registry.

When ProductManager returns task DAG JSON, the backend exposes the static `backend/app/static/backend_api_index.json` file containing id, title, and description only. ProductManager may add `backend_api_ids` to a task node. Before Builder runs that node, the backend resolves those ids from the static `backend/app/static/backend_api_context.json` file and appends only the selected entries to Builder input as `backend_api_context`. Scheduling is not a Builder backend API; recurring intent belongs in `manifest.json` schedule metadata.

The project-build workflow registry is backend-managed and contains only trusted checked-in executors. ProductManager selects a registered name; it cannot supply executable workflow code.

### CodexService

Acts as the Codex application facade for shared real/fake adapter integration, structured-output parsing, model routing, controlled workspaces, manifest helpers, and invocation safety. `ProductManagerContractService` separately normalizes untrusted ProductManager blueprint, permission, review, and task-DAG JSON into backend-owned allowlisted contracts. Project-build prompt composition is supplied by the owning workflow package. Update and standalone repair prompt composition remains role-based until those workflows are modularized. ProductManager Codex calls are forced to `read-only`; writable workflow/Builder/Tester calls are forced to `workspace-write` and scoped to controlled skill or draft-version directories.

`CodexInvocationRecorder` normalizes per-invocation token metadata into an adapter-neutral record, buffers build invocations until the active workflow step consumes them, and persists backend-mediated runtime success or failure records to the active `skill_runs` row. `AgentWorkflowService` attaches consumed build records to the active role step and maintains agent-run totals. Successful runtime calls update separate token totals; failed calls retain the resolved CLI path/version/source, exit code, error type, concise error detail, and a bounded stderr tail while leaving token totals at zero when no completed turn exists.

### CodexUsageService

Owns one persistent `codex app-server --stdio` child process for the FastAPI lifespan, performs JSON-RPC initialization, reads `account/rateLimits/read`, and normalizes the primary 300-minute and secondary 10,080-minute windows. Workflow pause checks are fail-open when allowance data is unavailable and pause only when Codex reports exhaustion.

### CodexCliService

Discovers Codex Desktop and PATH executables, probes their semantic versions, honors `PERSONAL_AGENT_CODEX_COMMAND` as a strict explicit override, selects the newest compatible automatic candidate, and supplies one resolved executable to every Codex-backed service. It exposes an optional minimum-version compatibility gate without owning future model or reasoning-effort selection policy.

### CodexRoutingService

Persists and validates unified invocation choices against the live App Server model catalog. Resolution precedence is invocation override, action or Builder-difficulty setting, role default, legacy environment default, then the catalog default. It returns requested and effective values plus the route source. The current provider is `codex_cli`; the contract keeps provider identity explicit so a future adapter can participate without being implemented here.

### PermissionService

Deterministically reviews permissions and dependencies. It creates approval requests, refreshes stale pending requests, approves/denies requests, detects permission expansion, checks install/run eligibility, and syncs waiting agent steps. Runtime `permissions.codex.call_response` is granted by default. Runtime `permissions.codex.internet_access` is supported only when the skill also has approved runtime `network` entries. Other Codex permission fields are blocked in this milestone.

### StaticCapabilityScanner

Performs the deterministic first stage of shared final validation for both Project build workflows. After the backend parses and package-checks the actual generated manifest, it scans bounded generated Python implementation files for selected direct network, process, browser, filesystem, sensitive-environment, deletion, and parseability evidence, compares recognized evidence with the manifest's runtime permission declarations, and persists a structured result. Blocking findings prevent backend test execution and runtime permission review. The single-Codex workflow blocks; the DAG workflow may ask Builder to repair before a rescan. The scanner is intentionally heuristic and does not replace manifest review or runtime containment.

### ProposedSkillService

Validates proposed skill packages, reads safe files, installs proposed skills, rejects or deletes proposed skills, handles approved build-time dependencies, and registers pending schedules declared in installed manifests. It does not expose a sample-skill or bare-record creation path.

### SkillRunner

Selects Docker or local/dev runner. Runners validate manifests, enforce supported permissions, run tests, execute entrypoints with JSON input, require JSON stdout, capture logs, and store `skill_runs`. Common nonzero exit codes produce specific error summaries for command failures, interruption, termination, forced kills, and segmentation faults while raw process diagnostics remain in stderr. A valid JSON process exit is not automatically treated as success: top-level `status: "partial"` and `status: "failed"` outputs become matching backend run statuses with a concise failure summary, and any recorded failed runtime Codex call prevents a succeeded run status. Runtime Codex usage is associated with the currently running row under the existing per-skill operation lock. Runners provide `PERSONAL_AGENT_SKILL_ID` and `PERSONAL_AGENT_BACKEND_URL` so skill code can call approved backend APIs without shell access.

## Skill Codex API

Installed enabled skills may call:

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

Backend checks installed/enabled status, runtime approval, manifest validity, `permissions.codex.call_response`, and whether requested Codex internet access is backed by approved runtime `network` entries. Shell access remains prohibited; skills must not call the Codex CLI directly.

Current PM-visible backend API catalog entries:

- `1`: Skill Codex Call API.
- `2`: Tool UI Schema API.

Inferred but not added yet: runtime cache helper API, runtime permission status API, skill run metadata API, and memory lookup API. These are not PM-visible until their contracts and permissions are designed.

### SkillVersionService

Creates version records, copies active versions into draft folders, validates versions, compares active/candidate files, creates runtime approval requests when permissions change, activates versions, and discards drafts.

### SchedulerService

Uses APScheduler to register approved schedules and trigger scheduled skill runs through the same safety path as manual runs. Manifest-declared schedules are registered as pending records when the proposed skill is installed; approval is still required before activation.
Schedule-mutating API routes use the FastAPI lifespan's shared APScheduler instance with their request-scoped database session, so approval, pause, resume, and deletion update the live scheduler immediately. Run-now is limited to active approved schedules.

### SkillOperationGuard

Implements local per-skill operation locks. It prevents overlapping operations on the same skill without blocking unrelated skills or chat.

## Backend Safety Rule

Frontend state is never the safety boundary. The backend must enforce status checks, permission checks, runner support, operation locks, and filesystem path boundaries.
