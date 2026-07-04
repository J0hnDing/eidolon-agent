# Agent Build and Repair Workflow

## Build Flow

1. User sends a Project-mode request.
2. Backend evaluates project plausibility through Codex-backed review.
3. Skill generation request is created.
4. ProductManager creates:
   - blueprint;
   - permission plan;
   - one or more milestones.
5. Backend writes workflow artifacts under `runtime/agent_runs/run_<id>/`.
6. Backend creates a build-time approval request.
7. User approves generation.
8. Backend resumes the agent run.
9. For each milestone:
   - Builder implements that milestone only.
   - Tester writes/updates tests and validates.
   - If tests fail, Builder repairs the same milestone.
   - After repeated failures, ProductManager stops the workflow.
10. After all milestones pass, ProductManager verifies the complete proposed package.
11. Backend creates runtime permission review from the actual manifest.
12. Chat shows completion summary and runtime approval.
13. Skill remains proposed until the user installs or rejects it.

## Repair Flow

Repair is for failed proposed/installed skills or failed runs. It creates a controlled repair agent run.

Installed-skill repair should work on a proposed repair copy, not mutate the active installed skill in place.

Repair uses:

- ProductManager repair blueprint;
- Builder repair step;
- Tester validation;
- runtime permission review if repaired manifest changes;
- final ProductManager summary.

## Approval Count

The workflow should be smooth after the single build-time approval. The user does not approve each role transition. Runtime approval is separate and happens after generated files exist.

## Failure Limits

One milestone may fail more than once, but after more than three failed attempts the workflow stops and surfaces a stuck summary rather than looping indefinitely.
