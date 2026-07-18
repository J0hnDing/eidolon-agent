# Eidolon TODO

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

## TODO-010 - Domain-Level Runtime Egress Enforcement

- **Priority:** High
- **Status:** Planned
- **Area:** Backend / sandbox networking
- **Rationale:** Runtime approvals name explicit server-side network domains, but any approved domain currently enables Docker bridge egress without domain-level filtering. Web-application browser traffic is separately blocked, and no-network application ingress is isolated, but approved server egress still has this disclosed enforcement gap.
- **Acceptance criteria:** Docker runtime egress is restricted to the approved manifest domains for both bounded functions and web applications; DNS rebinding, direct IP access, redirects, IPv4/IPv6 differences, and dependency traffic have explicit tested policy; the trusted gateway and scoped backend capability channel continue to work without granting internet access; local-development fallback remains clearly disclosed; and UI/runtime diagnostics report the effective enforcement mode.

## TODO-012 - Progress-Aware Workflow Budgets And Partial Recovery

- **Priority:** Medium
- **Status:** Planned
- **Area:** Backend / Codex workflow reliability
- **Rationale:** Action-specific hard timeouts bound individual Codex calls but cannot distinguish healthy ongoing work from an idle or stuck process, cap total workflow execution across many DAG calls, or recover useful workflow progress after an infrastructure interruption. These concerns need one coordinated execution-state design so recovery never treats partial generated output as validated or silently repeats completed work.
- **Acceptance criteria:** Codex JSONL and relevant tool/file activity are streamed and persisted as invocation progress; an idle timeout terminates invocations only after a documented period without meaningful activity while a separate hard deadline remains authoritative; each project workflow has a backend-owned total execution budget that excludes user approval and deliberate pause time and is checked before admitting new work; timeout records preserve partial events, usage, last meaningful activity, command context, and structured timeout type; Task-DAG recovery resumes from the last backend-validated workflow state without rerunning completed nodes or accepting unmerged partial node output; single-Codex recovery has an explicit safe policy for reusing or discarding its partial workspace and never marks it valid without full deterministic validation; Windows termination covers the complete spawned process tree; restart, cancellation, timeout, and retry behavior are idempotent and covered by tests; and the UI reports whether a run exceeded idle, invocation-hard, or workflow-budget limits and what recovery action is available.

## TODO-013 - Nested Function Invocation Policy

- **Priority:** Medium
- **Status:** Planned
- **Area:** Backend / function composition
- **Rationale:** Milestone 2 supports one direct caller-to-function hop and deliberately rejects a function invoked by another function or web application from invoking another target. Safe nesting needs explicit depth, cycle, budget, lock, approval, and audit-chain semantics rather than accidental recursive authority.
- **Acceptance criteria:** Define and enforce a bounded maximum depth; reject cycles deterministically; evaluate every direct caller-target edge independently; propagate only newly scoped child capabilities; preserve per-skill operation safety and total time/resource budgets across the chain; store an inspectable parent/child run chain; attribute runtime Codex usage to the causing function run; handle partial failure and cancellation without orphaned active runs; and cover direct, scheduled, backend, and web-application origins without inventing synthetic caller skills.

## TODO-014 - Memory Context And Long-Term Adaptation

- **Priority:** Medium
- **Status:** Planned
- **Area:** Memory / adaptation
- **Rationale:** Eidolon can store explicit editable memory and preserve skill/run history, but it does not yet select memory for chat or skills, capture structured outcome feedback, or turn accumulated evidence into safe adaptation proposals. Long-term usefulness must grow without silently collecting conversations, rewriting memory, or mutating active skills.
- **Acceptance criteria:** Memory retrieval uses an inspectable backend-owned selection policy and includes only user-owned facts relevant to the current context; the UI shows which facts were used and allows exclusion, correction, and deletion; users can attach structured feedback and outcomes to skill runs; adaptation proposals cite the memory, feedback, and run evidence that caused them; proposed memory changes require explicit confirmation; proposed skill changes enter the existing versioned update, validation, comparison, permission, and activation workflow; no adaptation installs, enables, schedules, or activates itself; evaluation compares a proposal with its active predecessor against preserved acceptance criteria and regression tests; rejected adaptations remain auditable without repeatedly resurfacing; and all adaptation state remains local and deletable.
