## TODO-002: Local Non-Agentic Model Adapter

- Priority: medium
- Category: feature
- Area: backend-model-adapters
- Dependencies: none
- Rationale: Some controlled artifact transformations should be able to use a selected local non-agentic model without going through the Codex agent workflow.

-Acceptance Criteria:
A backend adapter reads only the selected path and relevant context, invokes the configured local model, validates its output, and writes only the corresponding backend-controlled artifacts.

## TODO-007: Transactional Filesystem and Database Operations

- Priority: low
- Category: refactor
- Area: backend-lifecycle-reliability
- Dependencies: none
- Rationale: Skill install, update, activation, rejection, and deletion coordinate filesystem changes with SQLite commits without one transaction spanning both resources. Process interruption can leave recoverable but inconsistent partial state.

-Acceptance Criteria:
Destructive lifecycle operations use staging plus deterministic commit/rollback or recovery markers; restart recovery is tested; and failed operations leave a clear diagnostic state.

## TODO-010: Domain-Level Runtime Egress Enforcement

- Priority: high
- Category: feature
- Area: backend-sandbox-networking
- Dependencies: none
- Rationale: Runtime approvals name explicit server-side network domains, but any approved domain currently enables Docker bridge egress without domain-level filtering. Web-application browser traffic is separately blocked, and no-network application ingress is isolated, but approved server egress still has this disclosed enforcement gap.

-Acceptance Criteria:
Docker runtime egress is restricted to the approved manifest domains for both bounded functions and web applications; DNS rebinding, direct IP access, redirects, IPv4/IPv6 differences, and dependency traffic have explicit tested policy; the trusted gateway and scoped backend capability channel continue to work without granting internet access; local-development fallback remains clearly disclosed; and UI/runtime diagnostics report the effective enforcement mode.

## TODO-012: Progress-Aware Workflow Budgets And Partial Recovery

- Priority: medium
- Category: feature
- Area: backend-codex-workflow-reliability
- Dependencies: none
- Rationale: Action-specific hard timeouts bound individual Codex calls but cannot distinguish healthy ongoing work from an idle or stuck process, cap total workflow execution across many DAG calls, or recover useful workflow progress after an infrastructure interruption. These concerns need one coordinated execution-state design so recovery never treats partial generated output as validated or silently repeats completed work.

-Acceptance Criteria:
Codex JSONL and relevant tool/file activity are streamed and persisted as invocation progress; an idle timeout terminates invocations only after a documented period without meaningful activity while a separate hard deadline remains authoritative; each project workflow has a backend-owned total execution budget that excludes user approval and deliberate pause time and is checked before admitting new work; timeout records preserve partial events, usage, last meaningful activity, command context, and structured timeout type; Task-DAG recovery resumes from the last backend-validated workflow state without rerunning completed nodes or accepting unmerged partial node output; single-Codex recovery has an explicit safe policy for reusing or discarding its partial workspace and never marks it valid without full deterministic validation; Windows termination covers the complete spawned process tree; restart, cancellation, timeout, and retry behavior are idempotent and covered by tests; and the UI reports whether a run exceeded idle, invocation-hard, or workflow-budget limits and what recovery action is available.

## TODO-015: Implement intent refinement with memory retrieval and preference extraction

- Priority: medium
- Category: feature
- Area: Project build workflow
- Dependencies: none
- Rationale: The current pm_refine_intent step is intentionally a no-Codex passthrough placeholder. Replace it with grounded intent refinement that retrieves relevant explicit memory, fills missing request context without inventing requirements, and extracts applicable user preferences.

-Acceptance Criteria:
- Retrieve only relevant explicit user memory using a RAG-like selection approach.
- Fill downstream request context from grounded user messages and selected memory while preserving unresolved ambiguity.
- Extract applicable user preferences separately from factual context and retain source traceability.
- Keep irrelevant, expired, non-user-editable, and disallowed sensitive memory out of the refined intent.
- Persist an auditable intent artifact and pass a bounded refinement contract to pm_plan_build.
- Add focused tests for relevance selection, context filling, preference extraction, ambiguity preservation, and privacy boundaries.

## TODO-016: Add bounded symbolic resolution to the capability scanner

- Priority: low
- Category: refactor
- Area: Backend capability validation
- Dependencies: none
- Rationale: The intentionally small scanner now treats non-literal integration operation expressions as ambiguous so valid generated skills are not blocked. A future bounded analysis may recover high-confidence findings from simple aliases without making the scanner fail closed or replacing runtime authorization.

-Acceptance Criteria:
Resolve only explicitly bounded, high-confidence cases such as single-assignment literal aliases; retain passive behavior for ambiguous data flow; continue checking resolved literal operations against the actual manifest and approved build context; document limitations; and add positive and negative regression tests proving valid generated code is not blocked.

## TODO-018: Consolidate Task DAG execution, resume, and retry ownership

- Priority: high
- Category: refactor
- Area: Agent workflows
- Dependencies: none
- Rationale: TaskDagBuildWorkflow delegates through many private AgentWorkflowService methods and duplicates node state transitions across execute, resume, and retry paths.

-Acceptance Criteria:
One component owns the Task DAG node state machine; execute, resume, blocked-user-action, and retry share one node transition; retry resumes the intended node without restarting completed work; compatibility changes are explicit.

## TODO-020: Replace startup schema patching with versioned database migrations

- Priority: medium
- Category: refactor
- Area: Database
- Dependencies: none
- Rationale: ensure_local_schema has become a large set of conditional compatibility rewrites without an ordered migration ledger.

-Acceptance Criteria:
A versioned mechanism records every applied transition; supported databases upgrade deterministically; new installations reach the current schema; startup no longer accumulates conditional DDL rewrites.

## TODO-021: Upgrade React Router past the audited security advisories

- Priority: high
- Category: bugfix
- Area: Frontend dependencies
- Dependencies: none
- Rationale: React Router 6.30.4 is affected by moderate open-redirect/XSS and SSR hydration advisories; the supported fix requires a v7 migration.

-Acceptance Criteria:
Router packages resolve to an unaffected release; navigation, splat routes, and embedded web-app flows are regression tested; v7 behavior changes are handled intentionally; tests and production build pass.

## TODO-022: Add a reproducible Python dependency lock workflow

- Priority: medium
- Category: others
- Area: Backend dependencies
- Dependencies: none
- Rationale: Backend dependencies use broad minimum versions without a committed resolver lock, making environments and audits non-reproducible.

-Acceptance Criteria:
One lock tool and update command are documented; runtime and development dependencies are pinned transitively; fresh installation from the lock is verified; audit and update responsibilities are documented.

## TODO-023: Remove confirmed runner and API redundancies

- Priority: low
- Category: refactor
- Area: Runtime and API
- Dependencies: none
- Rationale: Local and Docker runners duplicate manifest, entrypoint, and run-finalization helpers, while the proposed-skill listing client and overlapping route appear unused.

-Acceptance Criteria:
Shared runner lifecycle behavior has one implementation without a reuse-only inheritance hierarchy; unused proposed-skill surfaces are removed after compatibility verification; provider errors move to a neutral contract; focused tests pass.

## TODO-024: Decompose oversized service and frontend orchestration modules

- Priority: medium
- Category: refactor
- Area: Architecture
- Dependencies: none
- Rationale: CodexService, AgentWorkflowService, SkillDetailPage, and UsageSettingsPage combine multiple independently changing responsibilities and repeated coordination logic.

-Acceptance Criteria:
Each extraction has a named owner and narrow contract; Codex adapters are separated from workflow facades; UI resource state is split into focused hooks or panels; generic abstractions require concrete reuse; behavior remains tested.

## TODO-026: Align ProductManager workflow test stubs with the current planning schema

- Priority: medium
- Category: test
- Area: ProductManager workflow validation
- Dependencies: none
- Rationale: The broader backend test run stops at test_product_manager_creates_blueprint_and_permissions_before_task_dag: DeterministicCodexStub returns a function blueprint with object input_schema/output_schema, which the current structured planning schema rejects. This failure was reproduced outside Act live-chat execution and prevents a green backend suite.

-Acceptance Criteria:
Deterministic ProductManager fixtures conform to the current planning transport contract; the affected planning/build workflow tests pass without consuming Codex account quota; remaining backend-suite failures are triaged and corrected or documented.

## TODO-027: Upgrade frontend test tooling past audited advisories

- Priority: high
- Category: bugfix
- Area: Frontend dependencies
- Dependencies: none
- Rationale: The frontend dependency audit still reports advisories in Vitest 3, @vitest/mocker, and tinypool after compatible updates. The available remediation requires a major test-tool upgrade, so it is separate from the Assistant UI reorganization. React Router has its own existing TODO-021.

-Acceptance Criteria:
Vitest, @vitest/mocker, and tinypool resolve to unaffected releases; any Node/Vite peer requirements are handled explicitly; the complete frontend test suite and production build pass; the lockfile records the reviewed versions without forced overrides.

## TODO-028: Verify WhatsApp account pairing and approved live messaging

- Priority: medium
- Category: test
- Area: WhatsApp integration
- Dependencies: none
- Rationale: Automated bridge, backend and Settings checks pass. Live end-to-end verification needs the user to link their account by scanning the Settings QR code.

-Acceptance Criteria:
Restart the backend and pair an account in Settings. Verify contacts, chats and available recent messages without marking read. Deny one send and verify no message is sent; approve one explicit test text and verify one accepted message. Verify restart restores pairing and disconnect removes local auth/cache.

## TODO-029: Add native Muse protocol support to the WhatsApp bridge

- Priority: medium
- Category: feature
- Area: WhatsApp / native agents
- Dependencies: none
- Rationale: Live investigation identified Muse at 1807055946647697@bot and verified the user phone-originated Hi through MCP. Pinned Baileys 7.0.0-rc14 cannot read Muse replies or send the required Muse envelope; upstream PR2857 explicitly excludes Muse WASA envelope/root-secret app-state synchronization. Read discovery, routing and sync reporting are fixed.

-Acceptance Criteria:
Use a reviewed pinned dependency or repository-owned adapter to implement and test the native Muse WASA envelope and required encrypted app-state secret handling. Verify real Muse replies and honest send acceptance. Support the requested dedicated fixed-Muse sending capability with standing authorization while keeping ordinary WhatsApp sends approval-gated. Keep credentials and root secrets out of tools, logs, and unencrypted storage; preserve existing pairing/history and uncertainty protection.
