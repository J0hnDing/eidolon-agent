# Working History

This file records notable implemented changes. Each entry must contain only:

- date,
- time,
- title,
- summary,
- limitations or future implementations.

Do not add separate area, intention, changed-files, or verification fields. Implementation and verification details that materially explain the result belong in the summary.

## Entry Format

```markdown
## YYYY-MM-DD HH:MM - Short Title

- Summary: Short title followed by the implemented behavior and material verification.
- Limitations/Future implementations: Known limitations and explicitly deferred work. Use `None known` when empty.
```

## Current Entries

## 2026-07-15 22:23 - Executable-Only Skill Contract

- Summary: Executable-only skill contract. Removed the former skill-kind field from manifests, SQLAlchemy records, generation requests, API schemas, ProductManager/Builder contracts, runner and scheduler branches, frontend types and displays, fixtures, and documentation. Every skill now requires a Python entrypoint and tests, while optional `SKILL.md` reusable instructions remain supported. The local SQLite migration drops both legacy columns and removes their exact keys recursively from persisted JSON artifacts; the current local database was migrated after confirming all existing rows used the executable variant. The installed GitHub Trending skill manifest was migrated and its 24 tests passed. Ruff passed, all 245 backend tests passed, all 5 frontend tests passed, and the frontend production build passed.
- Limitations/Future implementations: None known.

## 2026-07-14 23:02 - Backend Service And Frontend Page Decomposition

- Summary: Backend service and frontend page decomposition. Extracted task-DAG validation and ready-batch calculation into `TaskDagService`, run artifact persistence and interface-artifact validation into `AgentRunArtifactStore`, ProductManager output normalization into `ProductManagerContractService`, and separate build/runtime usage handling into `CodexInvocationRecorder`. `AgentWorkflowService` and `CodexService` remain orchestration facades and call the new narrow interfaces directly without private compatibility forwarders. Moved Chat conversation state into a tested hook, Chat presentation into a feature workspace, and Skill Detail update/version/schedule/validation/run panels into tested feature components while route pages retain API orchestration. Hook coverage also fixed the synthesized first chat not being persisted before its first edit. Ruff passed, all 258 backend tests passed, all 5 frontend feature tests passed, and the frontend production build passed.
- Limitations/Future implementations: Update and repair orchestration still shares the main workflow service, and some route-level Skill Detail mutations remain intentionally colocated because they coordinate several panels. Further extraction should be driven by a concrete feature boundary rather than file-size alone.

## 2026-07-14 01:06 - Skill Exit-Code Error Summaries

- Summary: Skill exit-code error summaries. Local and Docker skill runners now translate common nonzero process exit codes into specific Error messages for execution failure (`126`), missing commands (`127`), SIGINT interruption (`130`), SIGKILL (`137`), segmentation faults (`139`), and SIGTERM (`143`), with an explicit numeric fallback for other codes. Raw process output remains available in stderr. Ruff passed, the focused runner suite passed with 31 tests, all 253 backend tests passed, and `git diff --check` passed.
- Limitations/Future implementations: Exit codes identify the terminating condition but cannot always identify the external actor that sent a signal; stderr remains the detailed diagnostic source.

## 2026-07-14 01:35 - Deterministic Final Validation and Static Capability Scan

- Summary: Deterministic final validation and static capability scan. Both `single_codex` and `task_dag` now use one backend-owned, agent-free validator that parses and package-checks the actual manifest, compares scanned Python capability evidence with that manifest's runtime declarations, and runs generated tests before runtime permission review. The backend creates the skill's `tests/` directory before writable agents in both workflows and instructions require test authors to use that existing directory. Single-Codex writes tests in its one invocation and blocks on backend validation failure without calling more agents. DAG Tester still writes the final E2E test, now solely from blueprint acceptance criteria, before backend validation; only the DAG workflow may invoke its bounded Builder repair loop. The duplicate `final_e2e_expectations` DAG field and completed TODO-001 were removed. Ruff passed, the targeted workflow/capability suite passed with 8 tests, all 254 backend tests passed, and `git diff --check` passed.
- Limitations/Future implementations: The static scanner is intentionally heuristic and cannot prove the absence of dynamic, transitive, encoded, dependency-internal, non-Python, or runtime-constructed behavior. It excludes tests and dependency folders, and recognized imports can still produce conservative false positives. Final proposed-skill tests continue to execute through the existing host-side validation path rather than the Docker runtime sandbox.

## 2026-07-14 00:20 - Runtime Codex Failure Diagnostics

- Summary: Runtime Codex failure diagnostics. Failed backend-mediated skill Codex calls now persist a failed invocation record on the active `skill_run` with resolved CLI path/version/source, exit code, error type, concise error detail, bounded stderr tail, and zero tokens when no turn completed. SkillRunner now maps top-level `partial` and `failed` outputs to honest backend statuses and prevents a run with a recorded failed Codex call from being marked succeeded merely because the skill process returned valid JSON with exit code zero. Run-history API/UI contracts and documentation now expose the new status and diagnostics. Ruff passed, the focused runtime suite passed with 28 tests, the adapter regression suite passed with 16 tests, all 241 backend tests passed, the frontend production build passed, and `git diff --check` passed.
- Limitations/Future implementations: Existing completed runs cannot recover failed CLI stderr that was discarded before this change. Runtime Codex calls made outside an active executable skill run still cannot be attributed to run history.

## 2026-07-13 00:58 - Project Build Workflow Override

- Summary: Project build workflow override. Added a persistent Codex setting that keeps automatic ProductManager workflow selection by default or forces every new Project build through Simple (`single_codex`) or Task DAG (`task_dag`). Backend selection now applies the override after ProductManager blueprinting, records the effective workflow and selection source in the ProductManager step, and validates the effective workflow through the trusted registry. Added the Codex Settings control, backend regression coverage, and current-behavior documentation. Ruff passed, the focused routing/workflow suite passed with 43 tests, all 237 backend tests passed, the frontend production build passed, and `git diff --check` passed.
- Limitations/Future implementations: The override applies to new Project builds and does not rewrite workflows already persisted on existing agent runs.

## 2026-07-13 00:55 - Canonical Contracts, Structured TODO, And Ruff

- Summary: Canonical contracts, structured TODO, and Ruff. Replaced agent-run milestone compatibility fields and the duplicate retry route with canonical task-node names, added safe local-schema data copying from old columns, collapsed legacy disabled skill rows into `installed` plus `enabled = false`, removed the non-persisted deleted schedule status, and added regression tests for those contracts. Reworked `docs/todo.md` into stable items with Priority, Status, Area, Rationale, and Acceptance criteria; added the audited service/page decomposition, approval-scope, message-storage, filesystem-transaction, and parallel-execution work; clarified documentation ownership in `AGENTS.md` and `docs/README.md`; corrected the stale claim that `ProposedSkillService` creates sample skills; and tightened lifecycle, scheduling, and data-model documentation. Installed Ruff 0.15.21, added it to backend development dependencies, configured import and correctness checks, applied the initial clean baseline, and made Ruff-before-pytest a repository rule. Ruff passed, all 235 backend tests passed, the frontend production build passed, local documentation links resolved, and `git diff --check` passed.
- Limitations/Future implementations: The confirmed architectural work remains in `docs/todo.md`; service and page decomposition, first-class schedule approval scope, the chat message-storage boundary, transactional filesystem/database lifecycle operations, and real isolated-workspace DAG concurrency were intentionally not implemented in this change.

## 2026-07-12 23:50 - Repository Consistency And Safety Audit

- Summary: Repository consistency and safety audit. Removed the unused bare `POST /skills` record-creation path and the obsolete sample-skill UI/API that bypassed the Project-mode creation boundary, restricted skill PATCH requests to enabling or disabling installed skills, centralized declared-file checks in package validation, enabled SQLite foreign-key enforcement, routed schedule mutations through the live shared APScheduler instance, made global schedule approvals activate or deny the schedule itself, blocked run-now for non-active schedules, cleaned schedule approvals and stale skill links during deletion, removed unreferenced linear-workflow and chat-plausibility helpers, corrected version-cap guidance, and aligned lifecycle, workflow, backend, and TODO documentation. Final backend, generated-skill, frontend, manifest, OpenAPI, compilation, and diff verification completed successfully.
- Limitations/Future implementations: Architectural findings that are not unambiguous bugs remain report-only. In particular, the large workflow/Codex service classes, lightweight SQLite schema migration strategy, single-Codex final validation gap, and true parallel DAG execution remain unchanged.

## 2026-07-12 20:50 - Modular Project Build Workflows

- Summary: Modular project build workflows. Added ProductManager selection of a single backend-only `build_workflow` value, persisted it separately from `blueprint.json`, kept intent refinement, plausibility review, blueprint creation, permission planning, and build-time approval as the shared starting sequence, and routed post-approval execution through a trusted workflow registry. Colocated shared preflight, DAG, and single-Codex Markdown instructions and prompt composition with their owning workflow packages; `CodexService` now delegates project-build prompt construction while retaining shared invocation, parsing, routing, usage, workspace, and safety primitives. Moved existing DAG execution, pause/resume, and retry orchestration into the `task_dag` package, and added a `single_codex` package that gives Codex the approved blueprint and effective permissions for one planning, build, and test invocation. The workflow/chat regression suite passed with 85 tests, the final single-Codex end-to-end test passed and executed the generated JSON entrypoint, the frontend production build passed, and `git diff --check` passed.
- Limitations/Future implementations: Independent backend acceptance-criteria and authoritative test validation after `single_codex` completion remains deferred in `docs/todo.md`. Central manifest schema, required test-directory, and declared-file validation plus runtime permission review remain enforced.

## 2026-07-12 15:00 - Codex CLI Compatibility And Per-Task Model Routing

- Summary: Codex CLI compatibility and per-task model routing. Added centralized CLI discovery/version probing with strict command overrides, live App Server model and supported-effort discovery, persistent routing settings, independent Chat and ProductManager action routes, Builder `easy`/`medium`/`hard` difficulty routes, Tester task/final/update routes, pre-invocation model/effort validation, requested/effective routing metadata, and the Codex Settings UI. The task DAG continues to contain difficulty but no model ids. The final backend suite passed with 219 tests, focused routing tests passed, the live model-catalog probe succeeded, the frontend production build passed, and `git diff --check` passed.
- Limitations/Future implementations: Only the `codex_cli` provider is implemented. The local non-agentic model adapter remains deferred. Model selection requires the local Codex App Server catalog to validate explicit choices. Direct Chat applies its configured route but does not persist a build-style token or invocation audit record.

## 2026-07-12 14:45 - Skill Runtime Token History

- Summary: Skill runtime token history. Added adapter/model-aware runtime Codex invocation records and aggregate token columns to `skill_runs`, associated successful backend-mediated calls with the active per-skill run, added a Skill Run History tab to Agent Run detail, and removed the completed runtime-tracking TODO. Focused runtime tests passed with 11 tests, the full backend suite passed with 219 tests, the frontend production build passed, and `git diff --check` passed.
- Limitations/Future implementations: Direct Chat token tracking remains outside runtime history. Runtime Codex calls made outside an active executable skill run are not attributed to a run.

## 2026-07-12 01:39 - Project Build Context Optimization And GitHub E2E

- Summary: Project build context optimization and GitHub end-to-end correction. Compacted ProductManager, Builder, Tester, and repair contexts; enforced Tester ownership of test files; added focused Tester self-checks; adjusted generated-skill timeouts; and corrected duplicate GitHub Trending card normalization. Before the user reverted the first-turn intent-refinement skip and direct failed-task resume, an end-to-end run used 382,608 tokens versus a 684,621-token baseline, a 44.1% reduction. The corrected proposed skill passed platform validation, 24 tests, a live parser smoke test, the then-current 205-test backend suite, and `git diff --check`.
- Limitations/Future implementations: Re-measure token usage after the two reverts before treating the 44.1% reduction as representative. The generated GitHub skill remains proposed, disabled, uninstalled, unscheduled, and subject to runtime permission approval.

## 2026-07-11 02:38 - Codex Build Usage Tracking

- Summary: Codex build usage tracking. Added ProductManager, Builder, and Tester invocation token records, step/build totals, UI reporting, a persistent App Server allowance client, `/usage/codex`, parallel-aware ready-node batches, and workflow pause/resume below a 5% reserve in either allowance window. The full backend suite passed with 200 tests, focused usage/workflow tests passed with 36 tests, the frontend build passed, a live allowance probe succeeded, and `git diff --check` passed.
- Limitations/Future implementations: The scheduler batches parallel-safe nodes but does not yet execute shared-workspace nodes concurrently. Build totals intentionally exclude installed-skill runtime calls.

## 2026-07-09 00:50 - ProductManager Build Instruction Split

- Summary: ProductManager build instruction split. Separated blueprint/permissions from post-approval task-DAG planning, adopted flat runtime permission fields, removed ProductManager summary Codex calls and unsupported DAG-phase decisions, and aligned CodexService, AgentWorkflowService, PermissionService, tests, and documentation. Focused schema tests and the then-current 190-test backend suite passed.
- Limitations/Future implementations: Existing compatibility DB/API fields such as `requested_network_domains_json` remain and are populated from `permission_plan.runtime.network`. Unrelated generated files under `skills/proposed/weekly_github_trend_analyzer/` remain untracked.

## 2026-07-08 15:06 - Remove Hybrid Skill Variant

- Summary: Removed the hybrid skill variant, retained optional `SKILL.md` support for executable packages, and updated shared schemas, manifest validation, prompts, agent instructions, tool filtering, frontend choices, and documentation. Focused backend suites and the frontend build passed.
- Limitations/Future implementations: Superseded by the later executable-only skill contract.

## 2026-07-08 02:34 - Manifest Schedule Registration

- Summary: Manifest schedule registration. Moved schedule intent into ProductManager-owned manifest metadata, removed the PM-visible Scheduling API catalog entry, registered manifest schedules as pending during installation, and improved fake planning names for weekly GitHub Trending skills. Focused schedule/planning tests and the full backend suite passed.
- Limitations/Future implementations: Proposed skills generated before this change are not renamed in place.

## 2026-07-08 01:52 - Skill Codex API And DAG API Context

- Summary: Skill Codex API and DAG API context. Added manifest `permissions.codex`, `POST /skills/{skill_id}/codex`, backend API catalog ids, task-node `backend_api_ids`, selected Builder API context, runner backend URL variables, and an explicit DAG view in Agent Run detail. The full backend suite and frontend build passed, and Chrome UI end-to-end verification completed an approved task DAG.
- Limitations/Future implementations: Potential backend APIs for runtime cache, permission status, skill-run metadata, and memory lookup remain deferred until their contracts and permissions are designed.

## 2026-07-07 21:43 - Backend Manifest Skeletons

- Summary: Backend manifest skeletons. The backend now seeds and finalizes `manifest.json` from approved blueprint and permission artifacts, validates declared entrypoints, reports seeded manifest changes in fallback interface artifacts, and discourages over-splitting same-file DAG nodes. The targeted workflow suite passed.
- Limitations/Future implementations: None known.

## 2026-07-07 02:32 - DAG Build Workflow

- Summary: DAG build workflow. Replaced the linear milestone flow with explicit ProductManager phases, backend DAG validation, task artifacts, interface artifacts, node-specific tests, final end-to-end tests, task retry aliases, and task-node UI labels. The full backend suite and frontend build passed.
- Limitations/Future implementations: True concurrent isolated-workspace DAG execution remains deferred.

## 2026-07-04 13:09 - Two-Phase ProductManager Build Review

- Summary: Two-phase ProductManager build review. Added intent and plausibility review before blueprint/permission creation, a separate plausibility instruction, `needs_input` requests, same-chat clarification continuation, tool UI guidance, and pending-request tracking in Chat. Backend tests and the frontend build passed.
- Limitations/Future implementations: A durable backend conversation table may be needed if multi-device chat continuity becomes a requirement.
