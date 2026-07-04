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

Coordinates chat requests. Chat mode returns direct answers. Project mode creates generation requests and starts build-time approval planning. Backend keyword heuristics must not silently create or block skills.

### SkillPlanService and ProjectPlausibilityService

Use Codex adapters when available to classify project plausibility and generate initial skill plans. Fake adapters support tests and local development.

### AgentWorkflowService

Coordinates bounded agent workflows for build, repair, and update. It creates `agent_runs`, writes artifacts, starts steps, resumes after approval, loops through build milestones, handles repair attempts, and records final summaries.

### CodexService

Builds prompts from instruction files and calls Codex adapters. It owns real/fake Codex integration, proposed skill generation, milestone builds, repairs, update edits, and Tester test-writing calls.

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
