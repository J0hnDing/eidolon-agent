# Project TODO

This file is the source of truth for confirmed, unfinished project work. Completed items are removed and summarized in [working_history.md](working_history.md).

## Field Definitions

- **Priority:** `High` blocks an important safety or correctness guarantee; `Medium` improves maintainability or a meaningful capability; `Low` is useful but not urgent.
- **Status:** `Planned`, `In progress`, or `Blocked`.
- **Area:** The primary subsystem affected.
- **Rationale:** Why the work belongs on the roadmap.
- **Acceptance criteria:** Observable conditions required before the item can be removed.

## TODO-001 - Independent Single-Codex Final Validation

- **Priority:** High
- **Status:** Planned
- **Area:** Backend / project-build validation
- **Rationale:** The `single_codex` workflow currently relies on checks performed within the same Codex invocation that generated the package. Manifest schema, required test-directory, and declared-file existence are checked centrally, but final acceptance criteria and authoritative tests need an independent backend-owned pass.
- **Acceptance criteria:** The backend runs final acceptance-criteria and authoritative test validation after generation, persists the result, and blocks runtime permission review or installation when that independent validation fails.

## TODO-002 - Local Non-Agentic Model Adapter

- **Priority:** Medium
- **Status:** Planned
- **Area:** Backend / model adapters
- **Rationale:** Some controlled artifact transformations should be able to use a selected local non-agentic model without going through the Codex agent workflow.
- **Acceptance criteria:** A backend adapter reads only the selected path and relevant context, invokes the configured local model, validates its output, and writes only the corresponding backend-controlled artifacts.

## TODO-003 - Decompose Workflow and Codex Services

- **Priority:** Medium
- **Status:** Planned
- **Area:** Backend / maintainability
- **Rationale:** `AgentWorkflowService` and `CodexService` each combine several distinct responsibilities and are large enough that changes have a high review and regression cost.
- **Acceptance criteria:** Characterization tests protect current behavior; cohesive responsibilities are extracted behind narrow interfaces; workflow transaction, safety, routing, and usage-accounting behavior remains unchanged; and no compatibility forwarding layer is left without a documented removal plan.

## TODO-004 - Decompose Large Frontend Pages

- **Priority:** Medium
- **Status:** Planned
- **Area:** Frontend / maintainability
- **Rationale:** `SkillDetailPage` and `ChatPage` combine data loading, mutation orchestration, state transitions, and many presentation sections, making focused changes harder to review and test.
- **Acceptance criteria:** Page-level components retain route and orchestration ownership while cohesive panels and stateful flows move into tested components or hooks, with no user-visible behavior regression.

## TODO-005 - First-Class Schedule Approval Scope

- **Priority:** Medium
- **Status:** Planned
- **Area:** Backend / approval contracts
- **Rationale:** Schedule approvals are currently identified by `request_scope = runtime` plus `request_type = schedule`. A first-class schedule scope would make validation, querying, and documentation more precise.
- **Acceptance criteria:** Schedule approvals use one canonical scope contract; existing local rows are migrated safely; permission approval behavior remains separate; and backend, frontend, tests, and documentation agree on the new values.

## TODO-006 - Resolve Backend Message Storage Boundary

- **Priority:** Low
- **Status:** Planned
- **Area:** Backend and frontend / chat persistence
- **Rationale:** Chat history is currently frontend-local while the backend retains a compatibility `messages` table mainly for memory-source cleanup. The ownership boundary should be explicit rather than indefinite compatibility code.
- **Acceptance criteria:** Decide whether chat history is backend-persisted or frontend-local; migrate or remove the compatibility table and cleanup route accordingly; preserve explicit memory edit/delete behavior; and document the chosen single source of truth.

## TODO-007 - Transactional Filesystem and Database Operations

- **Priority:** Low
- **Status:** Planned
- **Area:** Backend / lifecycle reliability
- **Rationale:** Skill install, update, activation, rejection, and deletion coordinate filesystem changes with SQLite commits without one transaction spanning both resources. Process interruption can leave recoverable but inconsistent partial state.
- **Acceptance criteria:** Destructive lifecycle operations use staging plus deterministic commit/rollback or recovery markers; restart recovery is tested; active installed versions are never modified in place; and failed operations leave a clear diagnostic state.

## TODO-008 - Real Parallel DAG Execution

- **Priority:** Low
- **Status:** Planned
- **Area:** Backend / project-build execution
- **Rationale:** The scheduler is parallel-aware but executes each admitted ready-node batch serially in one shared skill workspace. Real concurrency requires isolated writes and deterministic integration.
- **Acceptance criteria:** Independent ready nodes produce measurable wall-clock concurrency; concurrent nodes cannot observe or overwrite unmerged changes; undeclared writes and merge conflicts block the affected batch; failed nodes do not merge partial output; retry does not repeat merged nodes; and current locks, permissions, token accounting, and quota-pause behavior remain enforced.

Suggested design:

1. Compute the complete ready-node batch from satisfied dependencies.
2. Admit only `parallel_safe` nodes whose declared `file_write_claims` do not overlap.
3. Create one isolated workspace per node.
4. Run Builder, Tester, and node-local tests in that workspace.
5. Compare actual changes with `file_write_claims` and reject unsafe or undeclared writes.
6. Merge successful outputs into the integration workspace in a stable order.
7. Revalidate interfaces, manifest, permissions, and integrated tests after each batch.
8. Persist per-node status, logs, failures, and token usage without sharing mutable database sessions or usage accumulators across workers.
9. Apply one quota decision to the admitted batch, then recheck reserves before admitting the next batch.
10. Clean up isolated workspaces safely on pause, failure, cancellation, or restart while preserving diagnostics.
