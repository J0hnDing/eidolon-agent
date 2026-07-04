# Agent Build and Repair Workflow

## Build Flow

1. User sends a Project-mode request.
2. Backend creates a generation request and ProductManager performs Phase 1 intent/plausibility review.
3. If the request is unclear, ProductManager asks a clarification question in the same chat and stops before artifact creation. The user's next Project-mode reply is appended to the same generation request, and ProductManager repeats Phase 1 with the full request context.
4. If the request is unsupported, ProductManager explains why and stops before artifact creation.
5. If the request is plausible, ProductManager continues to Phase 2.
6. ProductManager creates:
   - blueprint;
   - permission plan;
   - one or more milestones.
7. Backend writes workflow artifacts under `runtime/agent_runs/run_<id>/`.
8. Backend creates a build-time approval request.
9. User approves generation.
10. Backend resumes the agent run.
11. For each milestone:
    - Builder implements that milestone only.
    - Tester writes/updates tests and validates.
    - If tests fail, Builder repairs the same milestone.
    - After repeated failures, ProductManager stops the workflow.
12. After all milestones pass, ProductManager verifies the complete proposed package.
13. Backend creates runtime permission review from the actual manifest.
14. Chat shows completion summary and runtime approval.
15. Skill remains proposed until the user installs or rejects it.

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
