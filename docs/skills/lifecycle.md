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
2. ProductManager creates blueprint and permission artifacts.
3. The app creates a build-time approval request.
4. User approves generation.
5. Builder writes files inside `skills/proposed/<skill_name>/`.
6. Tester writes/runs tests.
7. ProductManager verifies completion.
8. Runtime permission review is created from actual `manifest.json`.
9. User inspects files and approvals.
10. User installs or rejects.

Generated skills are not installed or run automatically.

## Installation

Installing a proposed skill requires:

- valid manifest;
- applicable tests passing;
- runtime permissions reviewed and approved when required;
- supported runtime permissions;
- no conflicting per-skill operation lock.

Installed skills use versioned folders under `skills/installed/<skill_name>/versions/vN/`.

## Rejection and Deletion

Rejected proposed skills are hard-deleted in the local MVP: controlled folder removed and database record removed. Deleted skills should not remain visible in the normal Skills list.

Installed skill deletion is also a controlled hard delete and must respect operation locks.

## Manual Runs

Installed enabled automation/hybrid skills may run manually if:

- status is `installed`;
- `enabled` is true;
- `skill_type` is executable;
- runtime permissions are approved;
- runtime permissions are supported;
- no conflicting operation lock exists.

Instruction-only skills cannot run.
