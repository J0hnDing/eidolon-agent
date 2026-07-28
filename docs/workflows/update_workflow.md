# Skill Update Workflow

Updates are version-safe and do not use the new skill DAG build workflow in the MVP unless a future update explicitly opts into a small task DAG.

## Flow

1. User enters an improvement suggestion in Skill Detail.
2. ProductManager reviews the suggestion against current skill files and project rules.
3. ProductManager replies in the update chat with one of:
   - valid and build can start;
   - build-time approval required;
   - unclear and needs user input;
   - unsupported or blocked.
4. If build-time approval is required, backend creates a separate `backend` step and a `build_time` approval request with `request_type = "update"`, then pauses without relabeling the completed ProductManager step.
5. If approved, or if no extra build-time approval is needed, backend copies the active version into a draft/proposed version folder.
6. Builder modifies only the draft.
7. Tester writes/updates runtime-appropriate tests and validates the draft.
8. Permission review compares active and draft manifest/dependencies and the normalized integration contract.
9. If runtime permissions are unchanged, activation can skip runtime reapproval.
10. If permissions changed, user must approve runtime permissions before activation.
11. User compares, activates, or discards.

## Important Rules

- Active version is never edited in place. A running web application remains pinned to that active version while a draft is updated or repaired.
- Update/repair preserves the existing `function` or `web_app` execution protocol.
- Activating another web-app version stops old-version instances before the pointer changes; a later open starts the new version.
- No update auto-activates.
- No update auto-runs.
- No draft is created when ProductManager blocks/declines/asks for clarification.
- ProductManager decisions are user-facing summaries, not workflow errors.
- ProductManager receives the concise GitHub operation index. Builder and Tester receive detailed context only for operations selected in the update blueprint; Tester uses the deterministic fake adapter.
- Unchanged integration fingerprints may reuse authorization. Operation, resource-scope, registry-contract, or validated-account changes require a new integration decision before activation.

## Context Available to Agents

Update agents should see project-related files, including blueprint context, source snapshots, manifest, README, entrypoint, instructions, and tests when present.
