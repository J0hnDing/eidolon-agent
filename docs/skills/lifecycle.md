# Skill Lifecycle

## Statuses

```text
building -> proposed -> installed
                    -> failed
                    -> deleted
installed -> disabled
```

`building` means an agent workflow is creating or repairing the package. `proposed` means generation is complete and the skill is waiting for review, runtime permission approval, installation, or rejection.

## Proposed Skill Workflow

1. Project mode creates a generation request.
2. ProductManager refines intent and writes a plausibility decision.
3. If the request needs clarification, the same Project-mode chat continues the same generation request and ProductManager repeats intent/plausibility review.
4. If the request is plausible, ProductManager writes blueprint and permission artifacts.
5. The app creates a build-time approval request from the blueprint summary and permission plan.
6. User approves generation.
7. ProductManager writes a task DAG.
8. Backend validates the DAG and schedules ready task nodes.
9. Builder writes files inside `skills/proposed/<skill_name>/` for each task node.
10. Tester writes/runs node tests immediately after task builds that require tests.
11. Builder fixes failed task nodes until tests pass or failure limits block the workflow.
12. Tester writes and runs one final end-to-end test after all task nodes are done.
13. Builder fixes final end-to-end failures until tests pass or failure limits block the workflow.
14. Runtime permission review is created from actual `manifest.json`.
15. User inspects files and approvals.
16. User installs or rejects. If the installed manifest declares a schedule, the backend registers it as a pending schedule record during install.

Generated skills are not installed or run automatically. Manifest-declared schedules are not activated automatically; schedule approval is still separate from install approval.

## Installation

Installing a proposed skill requires:

- valid manifest;
- applicable tests passing;
- runtime permissions reviewed and approved when required;
- supported runtime permissions;
- no conflicting per-skill operation lock.

Installed skills use versioned folders under `skills/installed/<skill_name>/versions/vN/`.

If `manifest.json` contains `schedule`, install reads the installed manifest copy and creates a pending schedule plus schedule approval request. The backend does not ask Builder to write schedules through a separate backend API.

## Rejection and Deletion

Rejected proposed skills are hard-deleted in the local MVP: controlled folder removed and database record removed. Deleted skills should not remain visible in the normal Skills list.

Installed skill deletion is also a controlled hard delete and must respect operation locks.

## Manual Runs

Installed enabled automation skills may run manually if:

- status is `installed`;
- `enabled` is true;
- `skill_type` is executable;
- runtime permissions are approved;
- runtime permissions are supported;
- no conflicting operation lock exists.

Instruction-only skills cannot run.
