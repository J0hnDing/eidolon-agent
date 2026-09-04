# Skill Lifecycle

## Statuses

```text
building -> proposed -> installed
building -> failed
```

`building` means an agent workflow is creating or repairing the package. `proposed` means generation is complete and the skill is waiting for review, runtime permission approval, installation, or rejection. `installed` is the only normal installed lifecycle status; `enabled` independently controls whether an installed function can run or an installed web application can be opened. Service activation is represented only by its schedule status. `deleted` is retained only as a hidden legacy tombstone value, while current rejection and deletion paths hard-delete records.

## Proposed Skill Workflow

1. Project mode creates a generation request.
2. ProductManager refines intent once and starts a persistent `pm_plan_build` Codex App Server thread; the backend writes `decision.json` after each turn.
3. If the request needs clarification, the same Project-mode chat resumes that thread with the latest answer. If it is rejected, the thread is archived and no planning artifacts are created.
4. On `proceed_to_approval`, ProductManager returns the complete blueprint, permission plan, and workflow in one response; only then does the backend write planning artifacts and continue to approval. The thread is archived after this terminal planning outcome.
5. The app creates a build-time approval request from the blueprint summary and permission plan.
6. User approves generation.
7. Backend creates a clean proposed workspace, installs and verifies approved dependencies, and stops before Codex if provisioning fails.
8. Backend dispatches the approved build through the ProductManager-selected registered workflow:
   - `single_codex`: one controlled invocation plans, builds, writes tests, and performs its focused test run;
   - `task_dag`: ProductManager returns task DAG JSON, the backend validates it, and Builder/Tester execute its task nodes and final end-to-end loop.
9. Runtime permission review is created from the actual generated `manifest.json`.
10. User inspects files and approvals.
11. User installs or rejects. Installing a service creates its one required schedule in paused state. Function and web-app manifests cannot declare schedules.

Generated skills are not installed or run automatically. A generated service is initially disabled; the user enables it from Skill Detail or Schedules after runtime approval.

## Installation

Installing a proposed skill requires:

- valid manifest;
- tests passing;
- runtime permissions reviewed and approved when required;
- supported runtime permissions;
- no conflicting per-skill operation lock.

Installed skills use versioned folders under `skills/installed/<skill_name>/versions/vN/`.

Installation is idempotent after success and can recover an incomplete first copy left by an interrupted response. Package copying excludes Codex worktrees and pytest/Codex bookkeeping. The database switches to the installed version before source cleanup is treated as complete; a source folder that Windows cannot immediately remove is atomically moved outside active skill roots for deferred cleanup, so a locked transient directory cannot leave an installed folder paired with a proposed database record.

The already-provisioned runtime `.deps` folder is copied into the installed version. Build-only `.build-deps` is excluded. Installation validation verifies the existing dependency contract and never invokes `pip`.

If a service `manifest.json` contains its required `schedule`, install reads the installed manifest copy and creates one paused compatibility schedule while leaving the service disabled. Functions and web applications reject this field when non-null. The backend does not expose schedule creation to Builder or the UI.

## Rejection and Deletion

Rejected proposed skills are hard-deleted in the local MVP: the controlled folder and database record are removed.

Installed skill deletion is also a controlled hard delete and must respect operation locks. Active proposed/installed folders are first atomically moved outside skill roots, then running web application instances are stopped and their sessions/audit/instance rows are removed before the database record is committed. Deferred trash cleanup cannot turn a successful delete into a client-visible failure. If staging itself fails because another process holds the folder, the backend returns a controlled retryable error and preserves the record. Current delete paths do not transition a record to a `deleted` status.

## Manual Runs

Installed enabled function skills may run manually if:

- status is `installed`;
- `enabled` is true;
- runtime permissions are approved;
- runtime permissions are supported;
- no conflicting operation lock exists.

## Web Application Sessions

Installed enabled `web_app` skills are opened, not manually run. Opening requires the same runtime approval and supported-permission checks plus an active version. The backend lazily starts or reuses a healthy version-pinned instance, waits for readiness, and returns a distinct isolated browser session. Idle lifetime is not a `SkillRun` and holds no operation lock.

Disable stops active instances before persisting disabled state. Draft update/repair does not affect the active instance; activation stops old-version instances before switching the pointer. Delete, backend shutdown, idle cleanup, and startup recovery deterministically stop or reconcile runtime resources. See [Sandboxed web applications](../runtime/web_applications.md).

## Service Runs

Installed services use the same enabled/disabled availability control as other skills. Enabling registers their single required schedule; disabling pauses and unregisters it. Automatic execution and Run Now both require enabled state. Both paths recheck runtime permissions, integration authorization, input/output schemas, child-function availability, and operation locks. A disabled or deleted child function automatically disables any ancestor services. Service runs appear in ordinary run history with schedule attribution.

## Function Registry Calls

Installed function targets remain visible in the dynamic registry with explicit availability state. A function, scheduled service, or web application may invoke a target only when its active manifest declares the exact function requirement. Low-risk targets need no extra relationship approval; medium/high-risk targets require a current caller-target approval. The backend validates the complete function graph, derives transitive risk and permissions, and records parent/child run attribution. Disabled descendants make parents unavailable; missing, deleted, cyclic, or invalid descendants place parents in `error`. Every call still rechecks the immediate edge, caller and target lifecycle/version/runtime eligibility, schemas, operation locks, and approvals.
