# Backend API and Services

The backend is a FastAPI app in `backend/app/main.py`. Routers live under `backend/app/routers/`; services live under `backend/app/services/`.

## Main Routers

- `/chat`: normal chat and project-mode entry point, plus conversation history cleanup by frontend conversation id.
- `/memory-facts`: explicit user memory CRUD.
- `/skills`: skill CRUD, proposed skill workflow, runs, validation, install/reject, files, versions, schedules, repair, runtime permissions, and backend-mediated skill Codex calls.
- `/tools`: installed enabled tool skills and tool runs.
- `/schedules`: schedule list/detail/approve/deny/pause/resume/delete/run-now.
- `/permission-requests`: permission request list/detail/approve/deny.
- `/skill-generation-requests`: generation request list/detail/approve/deny/agent-run.
- `/agent-runs`: list/detail/steps/cancel/delete/resume/retry.

## Service Responsibilities

### ChatOrchestrator

Coordinates chat requests. Chat mode returns direct answers. Project mode creates generation requests, sends them through ProductManager intent refinement and plausibility review, and starts build-time approval planning only after ProductManager decides the request is ready to blueprint. If ProductManager asks for clarification, the next Project-mode chat reply is appended to the same generation request. Backend keyword heuristics must not silently create or block skills.

### SkillPlanService and ProjectPlausibilityService

Use Codex adapters when available to classify project plausibility and generate initial skill plans. Fake adapters support tests and local development.

### AgentWorkflowService

Coordinates bounded agent workflows for build, repair, and update. For builds, it records ProductManager intent refinement and plausibility steps before blueprint artifacts exist, pauses unclear requests with `needs_input`, writes `blueprint.json` and `permissions.json` only after a plausible review, requests deterministic build-time approval, writes `task_dag.json` only after approval, validates the DAG, schedules ready task nodes, starts Builder/Tester steps for each node, handles node fix loops, runs the final end-to-end Tester step, handles final fix loops, and records blocked summaries when needed.

When ProductManager writes `task_dag.json`, the backend exposes a small backend API index containing id, title, and description only. ProductManager may add `backend_api_ids` to a task node. Before Builder runs that node, the backend resolves those ids into full API context and appends it to Builder input as `backend_api_context`. Scheduling is not a Builder backend API; recurring intent belongs in `manifest.json` schedule metadata.

The build scheduler should be modular and called by the backend with the generation request, selected memory facts, approval state, project paths, Codex adapter, permission service, and proposed skill service. It should not be a generic workflow engine for unrelated applications.

### CodexService

Builds prompts from instruction files and calls Codex adapters. It owns real/fake Codex integration, ProductManager intent refinement, plausibility review, blueprint writing, permission-plan writing, task DAG writing, proposed skill task-node builds, task-node repairs, final end-to-end repairs, update edits, Tester test-writing calls, and backend-mediated skill Codex calls.

### PermissionService

Deterministically reviews permissions and dependencies. It creates approval requests, refreshes stale pending requests, approves/denies requests, detects permission expansion, checks install/run eligibility, and syncs waiting agent steps. Runtime `permissions.codex.call_response` is granted by default. Runtime `permissions.codex.internet_access` is supported only when the skill also has approved runtime network domains. Other Codex permission fields are blocked in this milestone.

### ProposedSkillService

Creates sample proposed skills, validates proposed skill packages, reads safe files, installs proposed skills, rejects/deletes proposed skills, handles approved build-time dependencies, and registers pending schedules declared in installed manifests.

### SkillRunner

Selects Docker or local/dev runner. Runners validate manifests, enforce supported permissions, run tests, execute entrypoints with JSON input, require JSON stdout, capture logs, and store `skill_runs`. Runners provide `PERSONAL_AGENT_SKILL_ID` and `PERSONAL_AGENT_BACKEND_URL` so skill code can call approved backend APIs without shell access.

## Skill Codex API

Installed enabled executable skills may call:

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

Backend checks installed/enabled status, runtime approval, manifest validity, `permissions.codex.call_response`, and whether requested Codex internet access is backed by approved runtime network domains. Shell access remains prohibited; skills must not call the Codex CLI directly.

Current PM-visible backend API catalog entries:

- `1`: Skill Codex Call API.
- `2`: Tool UI Schema API.

Inferred but not added yet: runtime cache helper API, runtime permission status API, skill run metadata API, and memory lookup API. These are not PM-visible until their contracts and permissions are designed.

### SkillVersionService

Creates version records, copies active versions into draft folders, validates versions, compares active/candidate files, creates runtime approval requests when permissions change, activates versions, and discards drafts.

### SchedulerService

Uses APScheduler to register approved schedules and trigger scheduled skill runs through the same safety path as manual runs. Manifest-declared schedules are registered as pending records when the proposed skill is installed; approval is still required before activation.

### SkillOperationGuard

Implements local per-skill operation locks. It prevents overlapping operations on the same skill without blocking unrelated skills or chat.

## Backend Safety Rule

Frontend state is never the safety boundary. The backend must enforce status checks, permission checks, runner support, operation locks, and filesystem path boundaries.
