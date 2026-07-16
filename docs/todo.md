# Project TODO

This file is the source of truth for confirmed, unfinished project work. Completed items are removed and summarized in [working_history.md](working_history.md).

## Field Definitions

- **Priority:** `High` blocks an important safety or correctness guarantee; `Medium` improves maintainability or a meaningful capability; `Low` is useful but not urgent.
- **Status:** `Planned`, `In progress`, or `Blocked`.
- **Area:** The primary subsystem affected.
- **Rationale:** Why the work belongs on the roadmap.
- **Acceptance criteria:** Observable conditions required before the item can be removed.

## TODO-002 - Local Non-Agentic Model Adapter

- **Priority:** Medium
- **Status:** Planned
- **Area:** Backend / model adapters
- **Rationale:** Some controlled artifact transformations should be able to use a selected local non-agentic model without going through the Codex agent workflow.
- **Acceptance criteria:** A backend adapter reads only the selected path and relevant context, invokes the configured local model, validates its output, and writes only the corresponding backend-controlled artifacts.

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

## TODO-009 - Dynamic Function Registry And Cross-Skill Invocation

- **Priority:** Medium
- **Status:** Planned
- **Area:** Backend / skill composition
- **Rationale:** The runtime discriminator now identifies bounded function skills, but discovery, typed registration, controlled cross-skill function calls, and any dedicated function-facing surface belong to Milestone 2 and were explicitly excluded from the web-application milestone.
- **Acceptance criteria:** Installed enabled function skills expose one backend-owned typed discovery contract; callers cannot trust arbitrary package commands or caller-supplied permissions; cross-skill calls enforce target approval, operation locks, bounded execution, input/output validation, version identity, and audit records; recursive or cyclic invocation is bounded; existing direct runs, schedules, and web applications remain compatible; and interface work is implemented only through an explicitly approved follow-up.

## TODO-010 - Domain-Level Runtime Egress Enforcement

- **Priority:** High
- **Status:** Planned
- **Area:** Backend / sandbox networking
- **Rationale:** Runtime approvals name explicit server-side network domains, but any approved domain currently enables Docker bridge egress without domain-level filtering. Web-application browser traffic is separately blocked, and no-network application ingress is isolated, but approved server egress still has this disclosed enforcement gap.
- **Acceptance criteria:** Docker runtime egress is restricted to the approved manifest domains for both bounded functions and web applications; DNS rebinding, direct IP access, redirects, IPv4/IPv6 differences, and dependency traffic have explicit tested policy; the trusted gateway and scoped backend capability channel continue to work without granting internet access; local-development fallback remains clearly disclosed; and UI/runtime diagnostics report the effective enforcement mode.
