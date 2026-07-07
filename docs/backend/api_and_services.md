# Backend API and Services

The backend is a FastAPI app in `backend/app/main.py`. Routers live under `backend/app/routers/`; services live under `backend/app/services/`.

## Main Routers

- `/chat`: normal chat and project-mode entry point.
- `/memory-facts`: explicit user memory CRUD.
- `/skills`: skill CRUD, proposed skill workflow, runs, validation, install/reject, files, versions, schedules, repair, runtime permissions.
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

The build scheduler should be modular and called by the backend with the generation request, selected memory facts, approval state, project paths, Codex adapter, permission service, and proposed skill service. It should not be a generic workflow engine for unrelated applications.

### CodexService

Builds prompts from instruction files and calls Codex adapters. It owns real/fake Codex integration, ProductManager intent refinement, plausibility review, blueprint writing, permission-plan writing, task DAG writing, proposed skill task-node builds, task-node repairs, final end-to-end repairs, update edits, and Tester test-writing calls.

### PermissionService

Deterministically reviews permissions and dependencies. It creates approval requests, refreshes stale pending requests, approves/denies requests, detects permission expansion, checks install/run eligibility, and syncs waiting agent steps.

### ProposedSkillService

Creates sample proposed skills, validates proposed skill packages, reads safe files, installs proposed skills, rejects/deletes proposed skills, and handles approved build-time dependencies.

### SkillRunner

Selects Docker or local/dev runner. Runners validate manifests, enforce supported permissions, run tests, execute entrypoints with JSON input, require JSON stdout, capture logs, and store `skill_runs`.

### SkillVersionService

Creates version records, copies active versions into draft folders, validates versions, compares active/candidate files, creates runtime approval requests when permissions change, activates versions, and discards drafts.

### SchedulerService

Uses APScheduler to register approved schedules and trigger scheduled skill runs through the same safety path as manual runs.

### SkillOperationGuard

Implements local per-skill operation locks. It prevents overlapping operations on the same skill without blocking unrelated skills or chat.

## Backend Safety Rule

Frontend state is never the safety boundary. The backend must enforce status checks, permission checks, runner support, operation locks, and filesystem path boundaries.
