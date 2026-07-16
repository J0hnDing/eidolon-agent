# Skill Lifecycle

## Statuses

```text
building -> proposed -> installed
building -> failed
```

`building` means an agent workflow is creating or repairing the package. `proposed` means generation is complete and the skill is waiting for review, runtime permission approval, installation, or rejection. `installed` is the only normal installed lifecycle status; `enabled` independently controls whether an installed function can run or an installed web application can be opened. `deleted` is retained only as a hidden legacy tombstone value, while current rejection and deletion paths hard-delete records.

## Proposed Skill Workflow

1. Project mode creates a generation request.
2. ProductManager refines intent and returns a plausibility decision; the backend writes the decision artifact.
3. If the request needs clarification, the same Project-mode chat continues the same generation request and ProductManager repeats intent/plausibility review.
4. If the request is plausible, ProductManager returns blueprint and permission JSON; the backend writes the artifacts.
5. The app creates a build-time approval request from the blueprint summary and permission plan.
6. User approves generation.
7. Backend dispatches the approved build through the ProductManager-selected registered workflow:
   - `single_codex`: one controlled invocation plans, builds, writes tests, and performs its focused test run;
   - `task_dag`: ProductManager returns task DAG JSON, the backend validates it, and Builder/Tester execute its task nodes and final end-to-end loop.
8. Runtime permission review is created from the actual generated `manifest.json`.
9. User inspects files and approvals.
10. User installs or rejects. If an installed function manifest declares a schedule, the backend registers it as a pending schedule record during install. Web applications cannot declare bounded-run schedules.

Generated skills are not installed or run automatically. Manifest-declared schedules are not activated automatically; schedule approval is still separate from install approval.

## Installation

Installing a proposed skill requires:

- valid manifest;
- tests passing;
- runtime permissions reviewed and approved when required;
- supported runtime permissions;
- no conflicting per-skill operation lock.

Installed skills use versioned folders under `skills/installed/<skill_name>/versions/vN/`.

Installation is idempotent after success and can recover an incomplete first copy left by an interrupted response. Package copying excludes Codex worktrees and pytest/Codex bookkeeping. The database switches to the installed version before source cleanup is treated as complete; a source folder that Windows cannot immediately remove is atomically moved outside active skill roots for deferred cleanup, so a locked transient directory cannot leave an installed folder paired with a proposed database record.

If `manifest.json` contains `schedule`, install reads the installed manifest copy and creates a pending schedule plus schedule approval request. The backend does not ask Builder to write schedules through a separate backend API.

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
