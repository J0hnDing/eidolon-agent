## 2026-07-18 00:36 — Eidolon Rename And Relocation Hardening

- Category: bugfix
- Area: unknown

### Summary

Eidolon rename and relocation hardening. Renamed the GitHub-facing project, visible application shell, API title, package metadata, agent instructions, default project-read label, and product-facing documentation to Eidolon. Rebuilt the root README around the local-first memory, self-built skill, permission-boundary, and long-term adaptation vision with an explicit implemented/partial/planned status matrix, architecture and capability-flow diagrams, setup instructions, and direct roadmap links. Kept `PERSONAL_AGENT_*`, `personal_agent.db`, browser-storage keys, internal gateway routes, and selected Docker resource identifiers as documented compatibility contracts so existing configuration and local state are not orphaned. Audited the move to `C:\Users\John\Projects\Eidolon`; no `.env` file or process environment value referenced the old path, active application roots resolve from the new location, and historical old-path text remains only in immutable logs/transcripts. Repaired the copied virtual environment's stale editable registration, added explicit setuptools package discovery so a fresh editable install works with the current backend layout, and confirmed `eidolon-backend` resolves to the new path. Ruff passed, all 319 backend tests passed from an external Windows pytest temp root, all 13 frontend tests passed, the frontend production build passed, all local Markdown links resolved, the active-root smoke check passed, and `git diff --check` passed.

### Limitations

Automatic memory selection, memory-aware responses, outcome feedback, and controlled long-term adaptation remain planned in TODO-014. Stable legacy compatibility identifiers intentionally remain internal. Historical runtime logs, generated prompts, and stored transcripts are not rewritten. The GitHub repository/remote URL was not renamed.

## 2026-07-17 02:51 — Function Registry And Risk-Based Cross-Skill Invocation

- Category: feature
- Area: unknown

### Summary

Function registry and risk-based cross-skill invocation. Added a backend-owned dynamic registry for installed function skills with active-version identity, validated object-shaped JSON input/output contracts, backend-derived risk, effective permissions, and explicit availability reasons while keeping it separate from the static trusted backend API catalog. Manifests and Project/update contracts now carry explicit caller `function_requirements`; build/runtime review presents each target and creates caller-specific approval requests only for medium/high-risk relationships, while low-risk declared targets need no redundant approval. Approval fingerprints cover target risk, permissions, dependencies, and JSON schemas so materially changed callable contracts become stale without invalidating code-only version updates. Manual runs, schedules, authenticated function callers, backend callers, and web applications converge on one registry service before the existing bounded runner; ephemeral run or instance capabilities prevent caller-supplied identity, input/output mismatches fail deterministically, and target runs record version, origin, caller, schedule/web-app attribution, errors, and separate runtime Codex usage. Added trusted function/web-app helper calls plus a transient allowlisted internal-network relay so no-internet Docker callers can reach only Function control-plane routes without receiving general egress. Preserved direct and scheduled execution for legacy schema-less functions while marking them unavailable as registry targets, removed the completed old registry TODO, added focused manifest/approval/schema/audit/helper/relay coverage, and updated architecture, manifest, lifecycle, security, runtime, frontend, workflow, and backend documentation. Ruff passed, all 319 backend tests passed, all 13 frontend tests passed, and the frontend production build passed.

### Limitations

Nested function calls are intentionally rejected and tracked in TODO-013. Legacy schema-less functions need an updated manifest before cross-skill use. There is no dedicated Function application or Tools UI. Domain-level filtering for otherwise approved Docker internet egress remains tracked in TODO-010, and the explicit `local/dev` runner remains less isolated than Docker.

## 2026-07-17 00:00 — Web Application Open And Modal Reliability

- Category: bugfix
- Area: unknown

### Summary

Web application open and modal reliability. `WebAppPage` now coalesces React Strict Mode mount replays so a cold application launch creates one session request instead of racing itself against the `web_app_start` lock. The trusted iframe and backend containment disclosure now allow modal dialogs while retaining the existing navigation, popup, download, browser-feature, and external-browser-network restrictions. The backend default-permission policy records the concise web-app-specific supported and blocked capability sets. Removed incomplete policy remnants that erased web-app network/Codex requests, blocked supported server-side capabilities, and silently resolved runtime approvals; web applications continue through the normal manifest-based approval path. Ruff passed, all 304 backend tests passed, all 13 frontend tests passed, and the frontend production build passed.

### Limitations

Concurrent session opens from separate browser contexts still use the backend operation-lock conflict contract; the duplicate request fixed here was the same-page Strict Mode replay.

## 2026-07-16 23:26 — Exact Agent Transcripts And Backend Step Identity

- Category: feature
- Area: unknown

### Summary

Exact agent transcripts and backend step identity. Agent Run steps now persist the exact fully composed prompt passed to each ProductManager, Builder, and Tester Codex invocation together with the exact final response returned by the adapter, while keeping backend-normalized workflow JSON separate for validation and orchestration. Build Details displays those raw text transcripts and explicitly marks unavailable historical values instead of presenting normalized JSON as agent I/O. Deterministic permission review, dependency provisioning, validation progression, workflow finalization, and bounded failure stops are recorded as `Backend` steps with a fixed summary and no input/output; approval linkage moved to a dedicated step column, and update approval no longer relabels a completed ProductManager invocation. Migrated Agent Run 1 so steps 4, 5, and 7 are Backend steps, recovered exact inputs for agent steps 1, 2, 3, and 6, and recovered exact outputs for steps 3 and 6; the overwritten historical outputs for steps 1 and 2 are honestly unavailable. Ruff passed across backend application and tests, all 302 backend tests passed, all 12 frontend tests passed, and the frontend production build passed. No Codex invocation or account-usage reset was used.

### Limitations

Agent Run 1 ProductManager outputs for intent refinement and plausibility review were overwritten before exact transcript persistence existed and cannot be reconstructed byte-for-byte.

## 2026-07-16 21:47 — Pre-Build Dependency Provisioning

- Category: feature
- Area: unknown

### Summary

Pre-build dependency provisioning. Build approval now covers both runtime and build-only requirements and immediately resumes into a backend-owned provisioning step before any post-approval ProductManager, Builder, or Tester invocation. The backend creates a clean workspace, atomically installs approved runtime packages into `.deps`, isolates missing build-only packages in `.build-deps`, verifies platform `pytest` availability and installed distribution versions, and gives Codex plus authoritative tests the same backend interpreter path and dependency `PYTHONPATH`. Repeated validation reuses and verifies the environment without invoking `pip`; manifest dependency drift fails before tests or runtime review. Removed the former late dependency installation and approval lookup from proposed-skill validation, removed its redundant tests and misleading approval language, and excluded build-only packages from scans, file reads, and installed versions. Added coverage for approval union, already-provisioned reuse, successful install, missing approval, unavailable packages, install failure and timeout, manifest drift, shared environment, and provisioning-before-workflow ordering. Ruff passed across backend application and test code, all 302 backend tests passed, and `git diff --check` passed.

### Limitations

Dependency provisioning currently applies to new Project builds; update and repair dependency changes retain their existing version-workflow behavior.

## 2026-07-16 14:39 — Action-Specific Codex Hard Timeouts

- Category: feature
- Area: unknown

### Summary

Action-specific Codex hard timeouts. Replaced the shared 300-second project-build Codex limit with backend-owned per-action limits: 120 seconds for ProductManager refinement and plausibility, 180 seconds for ProductManager blueprint/DAG/repair/update work, 600 seconds for Builder build/update/repair work, 300 seconds for Tester work, 900 seconds for the combined single-Codex build, and 45 seconds for bounded installed-skill Codex calls. Unknown legacy actions retain a 300-second compatibility fallback, and explicit adapter timeouts remain available for tests and embedding. Added policy, fallback, application, and explicit-override regression coverage and documented that limits apply per invocation rather than per workflow. Ruff passed across backend application and test code, all 296 backend tests passed, and `git diff --check` passed.

### Limitations

Progress-aware idle timeout, whole-workflow execution budgets, streamed partial diagnostics, and safe partial recovery remain deferred together in TODO-012.

## 2026-07-16 11:55 — Runtime-Only Interface Contract

- Category: feature
- Area: unknown

### Summary

Runtime-only interface contract. Removed the former interface selector and declarative tool UI schema from manifests, SQLAlchemy records, API and planning schemas, ProductManager/Builder/Tester contracts, generated manifest skeletons, persisted artifacts, fixtures, and frontend types. Removed the backend Tools routes and schemas, Tools list/detail pages, navigation, styles, tests, API catalog entry, and current-behavior documentation. Applications now use `runtime = web_app` as their sole visibility rule, while function skills receive no dedicated interface surface. The local SQLite migration dropped both retired skill columns and recursively cleaned their keys from persisted generation, version, agent, and approval artifacts; the current local database was migrated and verified with zero remaining retired contract keys. Both existing web-app package manifests validate under the new contract. Ruff passed, all 280 backend tests passed, all 12 frontend tests passed, the frontend production build passed, and `git diff --check` passed.

### Limitations

This change intentionally removed the prior function-facing interface; the later Function Registry milestone added machine-facing discovery without recreating a dedicated application UI.

## 2026-07-16 11:44 — Evidence-Based Static Capability Scanning

- Category: feature
- Area: unknown

### Summary

Evidence-based static capability scanning. Network and process imports no longer block without a recognized capability call, dynamic deletion targets no longer fail validation when their location cannot be proven, and literal deletion is blocked only when it is provably outside approved runtime write roots. Cache-local literal cleanup remains covered by `./cache` write permission. Added focused coverage for passive imports, dynamic temporary cleanup, allowed cache deletion, and blocked out-of-cache deletion; all 13 scanner tests passed, and the generated `simple_notes` package scans cleanly with zero findings.

### Limitations

The deliberately passive scanner may miss dynamic behavior; the runtime sandbox remains authoritative. A full backend run was attempted but could not provide a valid repository-wide result because concurrent unrelated workspace changes removed existing model fields and tool files during verification.

## 2026-07-16 11:19 — Terminal Single-Codex Error Handling

- Category: bugfix
- Area: unknown

### Summary

Terminal single-Codex error handling. Failed or blocked single-Codex builds can no longer resume, retry the current task, or retry a failed step, so an error never resends the original Builder prompt; task-DAG retry behavior remains unchanged. Agent Run detail hides both retry surfaces for terminal single-Codex errors. A separate new Project build atomically stages an obsolete proposed workspace before creating a clean one, preventing sandbox-owned `.pytest_cache` ACLs from causing raw Windows access-denied cleanup failures. Ruff passed, the 64-test focused workflow/lifecycle suite passed, all 283 backend tests passed, all 11 frontend tests passed, and the frontend production build passed.

### Limitations

Existing failed run and generated-package records remain historical and are not rewritten automatically.

## 2026-07-16 10:49 — Precise Network Import Capability Scanning

- Category: feature
- Area: unknown

### Summary

Precise network import capability scanning. Replaced top-level `urllib` classification with capability-bearing module-prefix matching so local helpers such as `urllib.parse` do not require runtime network permission, while `urllib.request`, direct URL-opening calls, and imported aliases remain detectable. Added paired regression coverage and confirmed the failed Notes Manager package now scans cleanly without broadening its empty network declaration. Ruff passed, all 9 focused scanner tests passed, and all 281 backend tests passed.

### Limitations

The scanner remains a bounded heuristic and does not infer behavior hidden behind dynamic imports, reflection, or dependencies.

## 2026-07-16 03:59 — Project Approval And Skill Lifecycle E2E Recovery

- Category: bugfix
- Area: unknown

### Summary

Project approval and skill lifecycle E2E recovery. Project chat now reconciles its transcript with persisted generation, build-approval, agent-run, proposed-skill, and runtime-approval state, including after a lost HTTP response, so approvals decided on the global Approval page reappear inline and generic build approval automatically resumes the waiting workflow. ProductManager output can no longer rename the backend-controlled skill, final validation rejects manifest name drift, and web application packages must use the platform-provided writable cache directory instead of a path beneath the read-only package mount. Proposed-skill tests now use an external temporary directory, package traversal skips inaccessible test artifacts, install copies only distributable files and atomically stages conflicting or obsolete trees, retries are idempotent, and delete commits independently of best-effort filesystem cleanup. Install and delete UI mutations also reconcile committed backend state after response loss. Recovered the interrupted `persistent_counter` install into a clean installed, disabled version, then verified its persisted counter across two Docker starts without enabling the application. Ruff passed, all 278 backend tests passed, all 9 frontend tests passed, and the frontend production build passed.

### Limitations

None known.

## 2026-07-16 01:59 — First-Class Sandboxed Web Application Skills

- Category: feature
- Area: unknown

### Summary

First-class sandboxed web application skills. Added a versioned manifest runtime contract for `function` and `web_app` packages, backend-derived risk classification, persistent web application instance/session/audit records, lazy version-pinned startup, per-session bearer origins, and short operation-lock transitions. Web applications run through a trusted ASGI host in hardened Docker containers by default, with read-only package/root filesystems, a writable per-skill cache, resource/capability limits, health checks, idle cleanup, startup stale-state recovery, and an explicitly labeled local-development fallback. Every Docker application starts on an internal network behind a trusted relay that bridges loopback ingress and only the exact scoped Codex-capability route; it receives neither skill code nor an instance token, while approved server egress is added separately. A same-origin gateway isolates all `*.web-app.localhost` traffic from the main API, applies iframe and browser containment headers, strips unsafe upstream behavior, caps request/response bodies, blocks WebSockets and literal browser-side external URLs, and exposes only a backend-mediated Codex capability that rechecks the active installed manifest and permissions. Added Web Applications list/detail routes, a sandboxed React iframe experience, package-file support across generation and version inspection, lifecycle cleanup for disable/activation/delete/shutdown, focused runtime and UI coverage, and authoritative architecture/security/API documentation. Ruff passed, all 270 backend tests passed, all 7 frontend tests passed, the frontend production build passed, live Docker image/start/readiness/HTTP/cleanup, no-egress/API-isolation, approved-egress, and scoped-capability relay probes passed, and the final repository checks passed.

### Limitations

Domain-level Docker egress enforcement remains deferred in TODO-010. Function registration and one-hop cross-skill invocation were completed in the later Function Registry milestone. WebSockets are intentionally unsupported. The explicit `local/dev` fallback is less isolated than Docker and is labeled accordingly.

## 2026-07-15 22:23 — Executable-Only Skill Contract

- Category: feature
- Area: unknown

### Summary

Executable-only skill contract. Removed the former skill-kind field from manifests, SQLAlchemy records, generation requests, API schemas, ProductManager/Builder contracts, runner and scheduler branches, frontend types and displays, fixtures, and documentation. Every skill now requires a Python entrypoint and tests, while optional `SKILL.md` reusable instructions remain supported. The local SQLite migration drops both legacy columns and removes their exact keys recursively from persisted JSON artifacts; the current local database was migrated after confirming all existing rows used the executable variant. The installed GitHub Trending skill manifest was migrated and its 24 tests passed. Ruff passed, all 245 backend tests passed, all 5 frontend tests passed, and the frontend production build passed.

### Limitations

None known.

## 2026-07-14 23:02 — Backend Service And Frontend Page Decomposition

- Category: refactor
- Area: unknown

### Summary

Backend service and frontend page decomposition. Extracted task-DAG validation and ready-batch calculation into `TaskDagService`, run artifact persistence and interface-artifact validation into `AgentRunArtifactStore`, ProductManager output normalization into `ProductManagerContractService`, and separate build/runtime usage handling into `CodexInvocationRecorder`. `AgentWorkflowService` and `CodexService` remain orchestration facades and call the new narrow interfaces directly without private compatibility forwarders. Moved Chat conversation state into a tested hook, Chat presentation into a feature workspace, and Skill Detail update/version/schedule/validation/run panels into tested feature components while route pages retain API orchestration. Hook coverage also fixed the synthesized first chat not being persisted before its first edit. Ruff passed, all 258 backend tests passed, all 5 frontend feature tests passed, and the frontend production build passed.

### Limitations

Update and repair orchestration still shares the main workflow service, and some route-level Skill Detail mutations remain intentionally colocated because they coordinate several panels. Further extraction should be driven by a concrete feature boundary rather than file-size alone.

## 2026-07-14 01:06 — Skill Exit-Code Error Summaries

- Category: bugfix
- Area: unknown

### Summary

Skill exit-code error summaries. Local and Docker skill runners now translate common nonzero process exit codes into specific Error messages for execution failure (`126`), missing commands (`127`), SIGINT interruption (`130`), SIGKILL (`137`), segmentation faults (`139`), and SIGTERM (`143`), with an explicit numeric fallback for other codes. Raw process output remains available in stderr. Ruff passed, the focused runner suite passed with 31 tests, all 253 backend tests passed, and `git diff --check` passed.

### Limitations

Exit codes identify the terminating condition but cannot always identify the external actor that sent a signal; stderr remains the detailed diagnostic source.

## 2026-07-14 01:35 — Deterministic Final Validation and Static Capability Scan

- Category: feature
- Area: unknown

### Summary

Deterministic final validation and static capability scan. Both `single_codex` and `task_dag` now use one backend-owned, agent-free validator that parses and package-checks the actual manifest, compares scanned Python capability evidence with that manifest's runtime declarations, and runs generated tests before runtime permission review. The backend creates the skill's `tests/` directory before writable agents in both workflows and instructions require test authors to use that existing directory. Single-Codex writes tests in its one invocation and blocks on backend validation failure without calling more agents. DAG Tester still writes the final E2E test, now solely from blueprint acceptance criteria, before backend validation; only the DAG workflow may invoke its bounded Builder repair loop. The duplicate `final_e2e_expectations` DAG field and completed TODO-001 were removed. Ruff passed, the targeted workflow/capability suite passed with 8 tests, all 254 backend tests passed, and `git diff --check` passed.

### Limitations

The static scanner is intentionally heuristic and cannot prove the absence of dynamic, transitive, encoded, dependency-internal, non-Python, or runtime-constructed behavior. It excludes tests and dependency folders, and recognized imports can still produce conservative false positives. Final proposed-skill tests continue to execute through the existing host-side validation path rather than the Docker runtime sandbox.

## 2026-07-14 00:20 — Runtime Codex Failure Diagnostics

- Category: bugfix
- Area: unknown

### Summary

Runtime Codex failure diagnostics. Failed backend-mediated skill Codex calls now persist a failed invocation record on the active `skill_run` with resolved CLI path/version/source, exit code, error type, concise error detail, bounded stderr tail, and zero tokens when no turn completed. SkillRunner now maps top-level `partial` and `failed` outputs to honest backend statuses and prevents a run with a recorded failed Codex call from being marked succeeded merely because the skill process returned valid JSON with exit code zero. Run-history API/UI contracts and documentation now expose the new status and diagnostics. Ruff passed, the focused runtime suite passed with 28 tests, the adapter regression suite passed with 16 tests, all 241 backend tests passed, the frontend production build passed, and `git diff --check` passed.

### Limitations

Existing completed runs cannot recover failed CLI stderr that was discarded before this change. Runtime Codex calls made outside an active executable skill run still cannot be attributed to run history.

## 2026-07-13 00:58 — Project Build Workflow Override

- Category: feature
- Area: unknown

### Summary

Project build workflow override. Added a persistent Codex setting that keeps automatic ProductManager workflow selection by default or forces every new Project build through Simple (`single_codex`) or Task DAG (`task_dag`). Backend selection now applies the override after ProductManager blueprinting, records the effective workflow and selection source in the ProductManager step, and validates the effective workflow through the trusted registry. Added the Codex Settings control, backend regression coverage, and current-behavior documentation. Ruff passed, the focused routing/workflow suite passed with 43 tests, all 237 backend tests passed, the frontend production build passed, and `git diff --check` passed.

### Limitations

The override applies to new Project builds and does not rewrite workflows already persisted on existing agent runs.

## 2026-07-13 00:55 — Canonical Contracts, Structured TODO, And Ruff

- Category: feature
- Area: unknown

### Summary

Canonical contracts, structured TODO, and Ruff. Replaced agent-run milestone compatibility fields and the duplicate retry route with canonical task-node names, added safe local-schema data copying from old columns, collapsed legacy disabled skill rows into `installed` plus `enabled = false`, removed the non-persisted deleted schedule status, and added regression tests for those contracts. Reworked `docs/todo.md` into stable items with Priority, Status, Area, Rationale, and Acceptance criteria; added the audited service/page decomposition, approval-scope, message-storage, filesystem-transaction, and parallel-execution work; clarified documentation ownership in `AGENTS.md` and `docs/README.md`; corrected the stale claim that `ProposedSkillService` creates sample skills; and tightened lifecycle, scheduling, and data-model documentation. Installed Ruff 0.15.21, added it to backend development dependencies, configured import and correctness checks, applied the initial clean baseline, and made Ruff-before-pytest a repository rule. Ruff passed, all 235 backend tests passed, the frontend production build passed, local documentation links resolved, and `git diff --check` passed.

### Limitations

The confirmed architectural work remains in `docs/todo.md`; service and page decomposition, first-class schedule approval scope, the chat message-storage boundary, transactional filesystem/database lifecycle operations, and real isolated-workspace DAG concurrency were intentionally not implemented in this change.

## 2026-07-12 23:50 — Repository Consistency And Safety Audit

- Category: research
- Area: unknown

### Summary

Repository consistency and safety audit. Removed the unused bare `POST /skills` record-creation path and the obsolete sample-skill UI/API that bypassed the Project-mode creation boundary, restricted skill PATCH requests to enabling or disabling installed skills, centralized declared-file checks in package validation, enabled SQLite foreign-key enforcement, routed schedule mutations through the live shared APScheduler instance, made global schedule approvals activate or deny the schedule itself, blocked run-now for non-active schedules, cleaned schedule approvals and stale skill links during deletion, removed unreferenced linear-workflow and chat-plausibility helpers, corrected version-cap guidance, and aligned lifecycle, workflow, backend, and TODO documentation. Final backend, generated-skill, frontend, manifest, OpenAPI, compilation, and diff verification completed successfully.

### Limitations

Architectural findings that are not unambiguous bugs remain report-only. In particular, the large workflow/Codex service classes, lightweight SQLite schema migration strategy, single-Codex final validation gap, and true parallel DAG execution remain unchanged.

## 2026-07-12 20:50 — Modular Project Build Workflows

- Category: feature
- Area: unknown

### Summary

Modular project build workflows. Added ProductManager selection of a single backend-only `build_workflow` value, persisted it separately from `blueprint.json`, kept intent refinement, plausibility review, blueprint creation, permission planning, and build-time approval as the shared starting sequence, and routed post-approval execution through a trusted workflow registry. Colocated shared preflight, DAG, and single-Codex Markdown instructions and prompt composition with their owning workflow packages; `CodexService` now delegates project-build prompt construction while retaining shared invocation, parsing, routing, usage, workspace, and safety primitives. Moved existing DAG execution, pause/resume, and retry orchestration into the `task_dag` package, and added a `single_codex` package that gives Codex the approved blueprint and effective permissions for one planning, build, and test invocation. The workflow/chat regression suite passed with 85 tests, the final single-Codex end-to-end test passed and executed the generated JSON entrypoint, the frontend production build passed, and `git diff --check` passed.

### Limitations

Independent backend acceptance-criteria and authoritative test validation after `single_codex` completion remains deferred in `docs/todo.md`. Central manifest schema, required test-directory, and declared-file validation plus runtime permission review remain enforced.

## 2026-07-12 15:00 — Codex CLI Compatibility And Per-Task Model Routing

- Category: feature
- Area: unknown

### Summary

Codex CLI compatibility and per-task model routing. Added centralized CLI discovery/version probing with strict command overrides, live App Server model and supported-effort discovery, persistent routing settings, independent Chat and ProductManager action routes, Builder `easy`/`medium`/`hard` difficulty routes, Tester task/final/update routes, pre-invocation model/effort validation, requested/effective routing metadata, and the Codex Settings UI. The task DAG continues to contain difficulty but no model ids. The final backend suite passed with 219 tests, focused routing tests passed, the live model-catalog probe succeeded, the frontend production build passed, and `git diff --check` passed.

### Limitations

Only the `codex_cli` provider is implemented. The local non-agentic model adapter remains deferred. Model selection requires the local Codex App Server catalog to validate explicit choices. Direct Chat applies its configured route but does not persist a build-style token or invocation audit record.

## 2026-07-12 14:45 — Skill Runtime Token History

- Category: feature
- Area: unknown

### Summary

Skill runtime token history. Added adapter/model-aware runtime Codex invocation records and aggregate token columns to `skill_runs`, associated successful backend-mediated calls with the active per-skill run, added a Skill Run History tab to Agent Run detail, and removed the completed runtime-tracking TODO. Focused runtime tests passed with 11 tests, the full backend suite passed with 219 tests, the frontend production build passed, and `git diff --check` passed.

### Limitations

Direct Chat token tracking remains outside runtime history. Runtime Codex calls made outside an active executable skill run are not attributed to a run.

## 2026-07-12 01:39 — Project Build Context Optimization And GitHub E2E

- Category: feature
- Area: unknown

### Summary

Project build context optimization and GitHub end-to-end correction. Compacted ProductManager, Builder, Tester, and repair contexts; enforced Tester ownership of test files; added focused Tester self-checks; adjusted generated-skill timeouts; and corrected duplicate GitHub Trending card normalization. Before the user reverted the first-turn intent-refinement skip and direct failed-task resume, an end-to-end run used 382,608 tokens versus a 684,621-token baseline, a 44.1% reduction. The corrected proposed skill passed platform validation, 24 tests, a live parser smoke test, the then-current 205-test backend suite, and `git diff --check`.

### Limitations

Re-measure token usage after the two reverts before treating the 44.1% reduction as representative. The generated GitHub skill remains proposed, disabled, uninstalled, unscheduled, and subject to runtime permission approval.

## 2026-07-11 02:38 — Codex Build Usage Tracking

- Category: feature
- Area: unknown

### Summary

Codex build usage tracking. Added ProductManager, Builder, and Tester invocation token records, step/build totals, UI reporting, a persistent App Server allowance client, `/usage/codex`, parallel-aware ready-node batches, and workflow pause/resume below a 5% reserve in either allowance window. The full backend suite passed with 200 tests, focused usage/workflow tests passed with 36 tests, the frontend build passed, a live allowance probe succeeded, and `git diff --check` passed.

### Limitations

The scheduler batches parallel-safe nodes but does not yet execute shared-workspace nodes concurrently. Build totals intentionally exclude installed-skill runtime calls.

## 2026-07-09 00:50 — ProductManager Build Instruction Split

- Category: feature
- Area: unknown

### Summary

ProductManager build instruction split. Separated blueprint/permissions from post-approval task-DAG planning, adopted flat runtime permission fields, removed ProductManager summary Codex calls and unsupported DAG-phase decisions, and aligned CodexService, AgentWorkflowService, PermissionService, tests, and documentation. Focused schema tests and the then-current 190-test backend suite passed.

### Limitations

Existing compatibility DB/API fields such as `requested_network_domains_json` remain and are populated from `permission_plan.runtime.network`. Unrelated generated files under `skills/proposed/weekly_github_trend_analyzer/` remain untracked.

## 2026-07-08 15:06 — Remove Hybrid Skill Variant

- Category: refactor
- Area: unknown

### Summary

Removed the hybrid skill variant, retained optional `SKILL.md` support for executable packages, and updated shared schemas, manifest validation, prompts, agent instructions, tool filtering, frontend choices, and documentation. Focused backend suites and the frontend build passed.

### Limitations

Superseded by the later executable-only skill contract.

## 2026-07-08 02:34 — Manifest Schedule Registration

- Category: feature
- Area: unknown

### Summary

Manifest schedule registration. Moved schedule intent into ProductManager-owned manifest metadata, removed the PM-visible Scheduling API catalog entry, registered manifest schedules as pending during installation, and improved fake planning names for weekly GitHub Trending skills. Focused schedule/planning tests and the full backend suite passed.

### Limitations

Proposed skills generated before this change are not renamed in place.

## 2026-07-08 01:52 — Skill Codex API And DAG API Context

- Category: feature
- Area: unknown

### Summary

Skill Codex API and DAG API context. Added manifest `permissions.codex`, `POST /skills/{skill_id}/codex`, backend API catalog ids, task-node `backend_api_ids`, selected Builder API context, runner backend URL variables, and an explicit DAG view in Agent Run detail. The full backend suite and frontend build passed, and Chrome UI end-to-end verification completed an approved task DAG.

### Limitations

Potential backend APIs for runtime cache, permission status, skill-run metadata, and memory lookup remain deferred until their contracts and permissions are designed.

## 2026-07-07 21:43 — Backend Manifest Skeletons

- Category: feature
- Area: unknown

### Summary

Backend manifest skeletons. The backend now seeds and finalizes `manifest.json` from approved blueprint and permission artifacts, validates declared entrypoints, reports seeded manifest changes in fallback interface artifacts, and discourages over-splitting same-file DAG nodes. The targeted workflow suite passed.

### Limitations

None known.

## 2026-07-07 02:32 — DAG Build Workflow

- Category: feature
- Area: unknown

### Summary

DAG build workflow. Replaced the linear milestone flow with explicit ProductManager phases, backend DAG validation, task artifacts, interface artifacts, node-specific tests, final end-to-end tests, task retry aliases, and task-node UI labels. The full backend suite and frontend build passed.

### Limitations

True concurrent isolated-workspace DAG execution remains deferred.

## 2026-07-04 13:09 — Two-Phase ProductManager Build Review

- Category: feature
- Area: unknown

### Summary

Two-phase ProductManager build review. Added intent and plausibility review before blueprint/permission creation, a separate plausibility instruction, `needs_input` requests, same-chat clarification continuation, tool UI guidance, and pending-request tracking in Chat. Backend tests and the frontend build passed.

### Limitations

A durable backend conversation table may be needed if multi-device chat continuity becomes a requirement.

## 2026-07-24 17:32 — Minimal GitHub Integration Capability

- Category: feature
- Area: backend-integrations

### Summary

Implemented trusted Windows Credential Manager-backed GitHub connection management; one typed six-operation read-only registry; manifest integration requirements; separate fingerprinted skill authorization; function and web-app capability relays; backend-only provider execution; sanitized auditing and errors; selected-only ProductManager, Builder, Tester, and single-Codex context; deterministic fake adapters; static direct-access validation; Settings and approval UI; focused regression and secret-leakage tests; and linked current-behavior documentation.

### Limitations

The explicit local/dev runtime fallback remains less isolated than Docker; production Docker runtimes block direct GitHub host resolution and all runtime integration calls are still enforced by the authenticated backend relay.

## 2026-07-29 19:12 — Unified function catalog and PM blueprint routing

- Category: feature
- Area: skills and agent workflows

### Summary

Removed SkillPlanService and the legacy backend API catalogs. ProductManager now refines intent, reviews plausibility, and writes the authoritative blueprint with function schemas and exact unified-catalog selections. The backend persists backend-core, user, and integration functions with live availability, derives manifest requirements, assigns selected functions through Task DAG nodes, and injects resolved full context into Builder or single-Codex prompts. Added the Functions UI plus an installed demo function and consuming web application. Ruff, 363 backend tests, 16 frontend tests, the frontend production build, and two sample tests passed.

### Limitations

Manual UI and runtime end-to-end testing was intentionally not run at the user request.

## 2026-07-29 23:33 — Restore GitHub function catalog availability

- Category: bugfix
- Area: integrations and function catalog

### Summary

Changed the unified function catalog to construct GitHub integration state through the production integration-service factory, preserving Windows Credential Manager availability checks. Extended the catalog lifecycle test to verify GitHub functions transition from unavailable to available when the connection becomes usable. Ruff and 39 focused catalog/integration tests passed.

### Limitations

No live GitHub API request was made.

## 2026-07-30 10:57 — Document function catalog extension flows

- Category: documentation
- Area: functions and integrations

### Summary

Added a concise function-extension blueprint covering installed user functions, new operations for an existing integration provider, entirely new providers, and backend-core functions. The guide distinguishes catalog metadata from callable implementation and keeps provider-specific behavior out of unrelated provider modules. Linked it from AGENTS.md, the documentation index, function runtime docs, and GitHub integration docs.

### Limitations

none

## 2026-07-30 11:43 — Single-Codex Builder model routing

- Category: feature
- Area: Codex settings and project builds

### Summary

Added a dedicated single_codex Builder routing choice with live model and reasoning-effort validation, backend action resolution, Settings UI controls, API typing, regression tests, and current-behavior documentation. Existing saved settings inherit the Builder default until the user chooses an override. Ruff passed, all 365 backend tests passed, all 17 frontend tests passed, the production frontend build passed, and git diff --check passed.

### Limitations

none

## 2026-08-02 16:10 — Simplify Task DAG node contract

- Category: refactor
- Area: backend-project-build-execution

### Summary

Replaced task-node title and summary with stable id plus task_prompt; unified expected_output_paths and file_write_claims as required write_paths; removed ProductManager-authored interface artifact expectations; trimmed Builder and Tester node projections; added the exact ProductManager output schema; updated backend validation, artifact ownership, fallback behavior, Agent Run UI, agent instructions, workflow documentation, and regression coverage. Ruff passed, 133 focused backend tests passed, all 380 backend tests passed, all 17 frontend tests passed, the production frontend build passed, and git diff --check passed.

### Limitations

Real isolated-workspace parallel execution remains deferred. Projector-managed TODO-008 still uses the retired file_write_claims term because the current agent API has no TODO edit route.

## 2026-08-02 16:48 — Config-derived agent permission policy

- Category: refactor
- Area: backend permissions and agent workflows

### Summary

Made backend/app/static/default_permissions.json the canonical agent-facing permission policy with default_allowed, requires_approval, and blocked sections. Plausibility now receives only blocked; blueprint and update ProductManager actions receive the full policy; structured permission output and backend sanitization derive from the approval template; and post-approval ProductManager, Builder, Tester, repair, update, and single-Codex paths receive effective permission_bounds with config-derived blocked values. Removed duplicated policy lists from instruction files, updated workflow/security/agent/backend documentation, and added regression coverage.

### Limitations

Deterministic backend enforcement remains authoritative, and approved network domains still are not domain-firewalled as documented. No skill was installed or run.

## 2026-08-02 17:22 — Permission schema derivation and Settings policy display

- Category: feature
- Area: backend permissions and frontend settings

### Summary

Made ProductManager permission-plan output schemas regenerate from the canonical requires_approval config template on every schema request, return independent deep copies, and enforce required nested fields, no unknown fields, and unique non-empty string lists. Added strict config-template validation. Added the read-only GET /settings/permission-policy contract and displayed the current default-allowed, approval-required, blocked, and web-application policy plus source path in Codex Settings without duplicating policy values in TypeScript. Updated security, backend API, and frontend documentation and added backend/UI regression coverage.

### Limitations

The policy remains checked-in and read-only in Settings; changing it still requires editing the canonical backend config and restarting/reloading the backend. Existing documented network-domain enforcement limitations remain unchanged.

## 2026-08-12 17:19 — Eidolon-Atlas integration and automatic unlock

- Category: feature
- Area: integrations

### Summary

Added Atlas as a trusted integration provider with filtered record and Knowledge operations, bounded Codex-backed Know node expansion, provider-aware manifests and approvals, audit and scanner protections, Windows Credential Manager API-key and passphrase storage, automatic local process startup and optional unlock, Settings controls, documentation, and focused validation.

### Limitations

Live Windows Credential Manager access, a real Atlas process attachment/startup, real Codex calls, browser end-to-end behavior, and the composite GitHub/Atlas analysis skill were not executed.

## 2026-08-18 00:29 — Made Codex model routing visibly saveable and verified invocation use

- Category: bugfix
- Area: frontend/settings

### Summary

Added an adjacent Save model routing action plus saved/unsaved status in Codex Settings. Strengthened regression coverage to prove a persisted single-Codex Builder route is reloaded and reaches the real Codex CLI model and reasoning-effort arguments.

### Limitations

In-app browser visual automation was unavailable in this run; DOM tests, the full frontend suite, production build, focused backend tests, Ruff, and diff checks passed.

## 2026-08-18 00:03 — Confine and simplify ProductManager build blueprints

- Category: bugfix
- Area: ProductManager workflow

### Summary

Replaced the unconstrained whole-blueprint transport workaround with a confined ProductManager schema that encodes only free-form input_schema, output_schema, and schedule.input leaves as JSON strings. Simplified build blueprints to name, description, runtime, input_schema, output_schema, expected_behavior, functions, and schedule; propagated the contract through planning, approval, permissions, builder/tester context, documentation, and regression tests. Failed planning can no longer create or approve an orphaned build request.

### Limitations

The transport removes unsupported uniqueItems keywords only from the Codex-facing schema; canonical backend validation remains authoritative. Existing unrelated worktree changes were left untouched.

## 2026-08-14 00:19 — Migrated Atlas integration to native unlocked API

- Category: refactor
- Area: Integrations and Settings

### Summary

Preserved all nine Atlas function contracts while replacing Agent calls with native record, Goal progression, Relationship, and Knowledge routes without credentials or Authorization headers. Added passphrase-only owned-process setup, external-process refusal and guidance, runtime-only availability, primary passphrase credentials, legacy key/passphrase cleanup with retry, authorization invalidation, native normalization coverage, frontend cleanup, and updated architecture/security documentation. Ruff passed, 439 backend tests passed, 18 frontend tests passed, production build passed, and live unlocked/locked runtime checks passed.

### Limitations

In-app browser visual QA was unavailable because the browser-control runtime failed to initialize; frontend component tests and the production build passed.

## 2026-08-23 17:55 — Polished frontend navigation and interaction system

- Category: feature
- Area: frontend-ui

### Summary

Reworked Eidolon's shared application shell into grouped icon-led navigation; introduced a cohesive responsive visual system for pages, cards, tables, forms, modals, chat, settings, and detail views; added subtle reduced-motion-aware transitions; improved chat deletion and approval-request selection; documented the current UI system; and added navigation regression coverage. Frontend tests and the production build pass.

### Limitations

Rendered in-app browser QA was unavailable because the browser bridge could not be established in this environment; validation used automated frontend tests, production build, and diff inspection.

## 2026-08-26 01:25 — Make agent cancellation terminal and recover Run 8

- Category: bugfix
- Area: agent workflow and runtime approvals

### Summary

Agent-run cancellation now terminates the owned Codex process, stays terminal across concurrent workflow errors, supersedes pending build approvals, and is atomic with successful finalization. Runtime approvals are gated until validation, fingerprinted against final manifest permissions and dependencies, and separated from exact integration approval requests in Skill Detail. Run 8 was deterministically revalidated and restored to succeeded/generated/proposed without rerunning Codex.

### Limitations

Notion integration approval request 57 remains pending for explicit user approval. Focused backend regressions, 24 frontend tests, frontend build, Ruff, and package validation pass; the full backend suite currently has 8 unrelated failures from concurrent MCP/function-catalog changes that reference missing test manifests.

## 2026-08-22 00:06 — Notion-backed todo integration

- Category: feature
- Area: integrations

### Summary

Added the sole-datastore Notion todo provider and domain TodoService, four generated-skill functions, fixed data-source containment, Windows Credential Manager connection lifecycle, separate approvals and audits, generated-code safeguards, connection-only Settings UI, exact setup documentation, and comprehensive backend/frontend coverage.

### Limitations

Requires a manually created and shared Notion data source with the documented exact schema and private connection capabilities; validation used deterministic provider tests rather than a live Notion credential.

## 2026-08-26 01:36 — Catalog-driven Codex MCP tools

- Category: feature
- Area: Function catalog, integrations, and Settings

### Summary

Added an official Python SDK STDIO MCP server that snapshots all available eligible catalog functions as stable typed Codex tools, rechecks availability and security-contract fingerprints per call, routes installed functions and trusted direct-user integrations through existing enforcement, records sanitized MCP audits, and supports immediately revocable enabled state. Added fingerprint-owned atomic global Codex TOML Install/Repair/Remove APIs and a Settings Integrations panel, with backend/frontend coverage, documentation, and a real MCP client smoke test.

### Limitations

New or restarted Codex Desktop, CLI, and IDE sessions are required to discover installation or catalog changes; open sessions are not hot-refreshed. Existing provider runtime and domain-egress limitations remain unchanged.

## 2026-08-28 13:12 — Fail closed when the real Codex CLI is unavailable

- Category: bugfix
- Area: Codex runtime

### Summary

Removed production FakeCodexAdapter and fake direct-chat fallback, made unavailable or disabled Codex fail closed, moved deterministic stubs under backend/tests/fakes, and replaced explicit full-workflow E2E coverage with focused adapter and manifest-repair tests.

### Limitations

Existing broader workflow component tests remain; no real Codex invocation test is part of pytest.

## 2026-08-27 21:57 — Notion Reports integration and deterministic weekly report service

- Category: feature
- Area: integrations-and-service-runtime

### Summary

Added separate Notion Reports source configuration and exact report list/get/create/delete contracts, raw native block handling, per-operation availability and MCP metadata, and a deterministic weekly GitHub Projects report service scheduled Monday 08:00 America/Toronto. Enabled unbounded declared function chains with new run-scoped child capabilities and direct-edge/target-owned checks, added regression coverage and documentation, and reconciled the installed service as paused.

### Limitations

The local Notion connection has no Reports data-source ID, so no live create/list/get/delete smoke write was performed. The weekly schedule remains paused and requires current runtime, function-edge, and Notion integration approval before resume. Cumulative transitive permission, budget, cancellation, cycle, and audit semantics remain tracked in TODO-025.

## 2026-08-27 16:16 — First-Class Schedule Approval Scope

- Category: refactor
- Area: backend-approval-contracts

### Summary

Completed by replacing function/web-app schedule approvals with the service-only scheduling contract. Generated services now own one required active/paused schedule, legacy schedule approvals are retired, and backend, frontend, tests, and documentation use the new contract.

### Limitations

The nullable physical approval_requests.schedule_id column remains for local database compatibility but is no longer used by application behavior.

## 2026-08-29 00:57 — Google Calendar OAuth integration

- Category: feature
- Area: integrations

### Summary

Added one trusted Google Calendar OAuth connection with exactly five typed primary-calendar event functions: create, list, get, update, and delete. OAuth uses bounded single-use state, write-only client credentials, Windows Credential Manager refresh-token storage, verified account identity, sanitized callbacks and access-log redaction. Added the Settings connection panel, catalog and manifest integration, provider containment, focused backend and frontend tests, and integration/backend/frontend/security/function-extension documentation.

### Limitations

No live Google OAuth or Calendar API E2E test was run; provider behavior is covered by deterministic focused tests. The integration intentionally excludes secondary calendars, event storage, synchronization, webhooks, schedules, calendar UI, and token revocation.

## 2026-08-29 22:56 — Fix Telegram re-pair race and system-time display

- Category: bugfix
- Area: Telegram integration and frontend time display

### Summary

Fixed an in-flight Telegram long-poll race that could compare a fresh pairing command against stale pairing state and consume it without binding. Polling now refreshes and validates the current bot generation before handling updates. Unified user-facing timestamps through a system-time formatter that treats offset-less backend timestamps as UTC and renders the host timezone abbreviation; the live Telegram bot was successfully paired after restart.

### Limitations

none

## 2026-08-30 13:44 — At-most-once latest missed schedule execution

- Category: feature
- Area: backend scheduling

### Summary

Added durable schedule runtime state and occurrence claims for generated and platform schedules. Startup now runs only the latest unclaimed due occurrence, normal callbacks share the same deterministic idempotency key, failures and interrupted occurrences are never retried, interval anchors persist across restarts, and scheduled run metadata plus the non-secret idempotency key are exposed to local and Docker service runtimes and run details. Updated scheduling, data-model, and backend documentation with focused backend and frontend validation.

### Limitations

none

## 2026-08-30 00:32 — Backend-owned approval and notification presentation

- Category: feature
- Area: Invocation approvals and Telegram

### Summary

Added a persisted backend approval-presentation model with an email.send preset and readable generic fallback, kept reason_to_call caller-supplied, updated Telegram notifications, and edit approval messages to terminal executed, denied, or failed outcomes with sanitized error explanations. The local approval page now consumes the same backend presentation instead of recognizing function ids.

### Limitations

Full backend collection is currently blocked by the unrelated missing GOOGLE_OAUTH_CALLBACK_PATH export. The full frontend suite retains three unrelated schedule, settings-loading, and Strict Mode failures. Approval-specific backend tests, related integration tests, Ruff, and the frontend production build pass.

## 2026-08-29 23:22 — Unify Google OAuth client with independent service grants

- Category: feature
- Area: integrations/settings

### Summary

Refactored Google Calendar and Gmail to share one write-only Google OAuth application client while retaining independent service grants, scopes, refresh tokens, account identities, connect/disconnect controls, and callbacks. Combined both services into one Google Settings section and preserved the existing Calendar connection through a safe legacy credential migration.

### Limitations

none

## 2026-08-29 18:12 — Telegram invocation approvals and Gmail integration

- Category: feature
- Area: integrations and invocation safety

### Summary

Implemented durable per-call invocation approvals with local and Telegram decisions, Telegram notification and approval pairing/polling, independent Gmail OAuth and provider-neutral email operations, shared effective callable contracts, provider containment, Settings and approval UI, documentation, and focused verification.

### Limitations

Gmail uses the restricted gmail.modify scope and therefore requires appropriate Google OAuth consent/testing-user configuration. Approval previews intentionally send complete bounded action content to Telegram. V1 activates one notification_approval bot; the persistence model supports future bot roles.

## 2026-08-30 18:28 — Reconstruct persistent Act mode and Telegram Agent

- Category: feature
- Area: Act runtime

### Summary

Rebuilt Act as a durable queued App Server workflow with enforced workspace boundaries, current MCP readiness checks, restart recovery, cancellation, hardened bounded downloads, independent Telegram Agent polling and delivery, and polling-based frontend sessions. Added focused backend and frontend coverage and updated current-behavior documentation.

### Limitations

none

## 2026-08-30 23:54 — Removed direct Chat mode

- Category: refactor
- Area: frontend-and-backend-conversation-workflow

### Summary

Removed the direct Codex Chat mode from the frontend and backend, deleted its execution adapter and response contract, made Project the default local conversation, retained only Project and Act creation, discarded legacy direct-chat rows instead of converting them into projects, removed Chat model routing while preserving independent Act routing, and aligned tests and documentation. Ruff passed, all 581 backend tests passed, all 32 frontend tests passed, the frontend production build passed, OpenAPI exposes no chat mode field, and git diff checks passed.

### Limitations

none

## 2026-08-31 00:56 — Dark-first control-plane UI and appearance settings

- Category: feature
- Area: frontend

### Summary

Replaced the layered frontend styling with a cohesive graphite and restrained-blue design system using bundled Plus Jakarta Sans and IBM Plex Mono, adopted line-led spatial segmentation and dot-plus-text statuses, integrated the Eidolon logo while preserving navigation icons, polished Chat with fixed-height conversation rows and desktop workspace plus neutral Project and Act symbol labels, and added persisted Dark and Light appearance selection in Settings. Added focused theme coverage, updated frontend documentation, and completed live desktop and compact dark-mode QA.

### Limitations

Light mode is implemented but was not visually QA tested, as requested.

## 2026-08-31 14:55 — Five-minute skill and nested capability timeouts

- Category: bugfix
- Area: runtime

### Summary

Raised bounded skill entrypoint execution from 120 to 300 seconds, aligned nested function and Codex capability clients and trusted relays at 300 seconds, and removed the skill_runtime_codex 45-second action override so it inherits the general five-minute Codex timeout. Updated focused tests and runtime/Codex documentation.

### Limitations

The 300-second entrypoint limit remains the total budget for a bounded skill process, so nested work shares that parent budget rather than extending it.

## 2026-09-01 01:43 — Nested Function Invocation Policy

- Category: research
- Area: backend-function-composition

### Summary

Superseded the old bounded-depth policy with a backend-validated finite acyclic function graph. Every edge remains independently authorized and scoped, nested runs retain per-skill locks and runner bounds, and parent_run_id provides an inspectable audit chain across direct, scheduled, backend, and web-application origins.

### Limitations

The historical numeric-depth requirement is intentionally superseded: valid graphs have no arbitrary depth cap, while cycles and missing children fail closed.

## 2026-09-02 12:54 — Separate function catalog reads from installed-skill reconciliation

- Category: refactor
- Area: Function catalog

### Summary

Separated UI catalog and skill-list reads from installed-skill reconciliation. Startup now performs reconciliation and builds the catalog projection; skill lifecycle, runtime approval, active-version, Atlas, and integration mutations refresh it. Existing projection reads perform no filesystem reconciliation or provider probing.

### Limitations

A missing catalog file is rebuilt once as a defensive bootstrap for service entry points that bypass application startup.

## 2026-09-01 01:43 — Define transitive permission and execution semantics for unbounded function chains

- Category: research
- Area: backend-function-composition

### Summary

Implemented and documented transitive function semantics: parent risk is at least every descendant and integration risk; runtime review includes the transitive child-permission union while each runner retains target-owned permissions; integration leaves add no ordinary permissions; graph fingerprints invalidate stale approvals; child disable/delete propagates disabled/error availability; cycles fail deterministically; nested runs persist parent_run_id; and each node keeps its bounded timeout/resource contract with synchronous child failure handling.

### Limitations

There is deliberately no separate chain-wide cancellation token or shared resource pool. Each node retains its existing bounded runner limits, and the immediate caller owns child-failure handling.

## 2026-09-03 02:57 — Quercus knowledge synchronization

- Category: feature
- Area: Act and integrations

### Summary

Added trusted Quercus token and course settings, incremental backend mirroring into runtime/act/knowledge/quercus, safe bounded Canvas transport and file downloads, selected-course retention/removal, the daily Toronto platform schedule, the three-directory Act contract, Settings and Schedules UI, documentation, and focused/full regression coverage.

### Limitations

Act knowledge is instruction-read-only until TODO-026 adds filesystem enforcement; files larger than 8 GiB or with unknown size remain metadata-only and have no lazy-fetch endpoint.

## 2026-09-03 19:53 — Add Quercus raw and processed file mirror

- Category: feature
- Area: Quercus integration

### Summary

Added database-owned Quercus processing settings and per-file/per-course status, migrated mirrored originals into files/raw, added deterministic files/processed Markdown output, trusted sequential Marker plus Surya llama.cpp execution with atomic retryable placeholders, dynamic Act guidance, settings APIs and UI, and independent synchronization/processing reporting. Verified the existing ECO101 mirror migrated 48 downloaded files with no workspace metadata.

### Limitations

Marker is not currently available on the backend process PATH, so processing remains disabled by default and the live processed tree is empty until the user installs Marker and enables the method in Settings. Knowledge read-only enforcement remains instruction-only under the existing high-priority TODO.

## 2026-09-05 21:35 — Show Observer and Assistant sessions in Chat

- Category: feature
- Area: frontend/chat

### Summary

Chat now loads and continues shared Observer and Assistant sessions alongside Act, using their backend APIs for creation, turns, cancellation, and archiving. New Chat opens a keyboard-accessible side drawer offering Create Project, Act, Observer, and Assistant. Observer icons are green and Assistant icons purple in Chat and Agents. Updated frontend behavior documentation and focused tests.

### Limitations

Live browser visual verification was not performed.

## 2026-09-05 14:37 — Unified actions for all scheduled services

- Category: feature
- Area: Scheduling

### Summary

Added durable enable/disable, Run Now, and schedule editing for backend-owned platform services; unified Assistant assessment controls with the Agents endpoints; updated UI, API contracts, persistence migration, documentation, and regression coverage.

### Limitations

Assistant assessment recurrence remains backend-defined; its Edit action opens Agents > Assistant. Backend-owned platform schedules do not accept input payloads.

## 2026-09-06 22:36 — Enforce Act knowledge directory as read-only

- Category: bugfix
- Area: Act sandbox

### Summary

Implemented backend-supplied Codex named permission profiles for managed agents. All roles receive read access to runtime/act; only Act receives write access to runtime/act/memory and runtime/act/workspace, leaving runtime/act/knowledge read-only to agents while backend synchronizers retain host write authority. Updated focused contract tests and Codex integration documentation; Ruff, 17 focused tests, 726 full backend tests, App Server profile initialization, and diff checks passed.

### Limitations

none

## 2026-09-06 15:37 — Provider-neutral integration authorization architecture

- Category: refactor
- Area: backend/integrations

### Summary

Replaced the centralized provider-specific catalog execution path with a checked-in provider-neutral operation registry, canonical effects/risk/typed resources, centralized per-invocation capability policy, secret-free authorized invocation objects, a runtime/provider adapter boundary, effect-derived catalog and agent projections, risk-derived HIGH approval handling, compatible standing-authorization fingerprint migration, and architecture regression coverage for all catalog-backed providers.

### Limitations

Existing non-GitHub integrations retain unrestricted provider-local resource scope because their prior behavior had no narrower caller-selected resource restriction. IntegrationService remains as a compatibility facade for settings, OAuth, connection lifecycle, and legacy callers; production catalog execution no longer uses it as an authorization or provider-dispatch monolith.

## 2026-09-06 02:49 — Unified catalog callable execution kernel

- Category: refactor
- Area: backend execution and authorization

### Summary

Introduced immutable InvocationContext, authenticated context factories, category-qualified InvocationTargetRef, InvocationExecutor, and handlers for user functions, integrations, backend-core functions, and agent-private functions. Migrated MCP, runtime, web-app, manual, internal backend, and claimed approval dispatch through the kernel; removed ambient and duplicate caller abstractions and direct production dispatch paths; preserved approval recovery, caller reauthorization, audits, schemas, and runtime permission checks. Added focused architecture tests and updated execution, MCP, and approval documentation. Validation: Ruff passed; full backend suite passed 688 tests with one existing deprecation warning; application import, compileall, production bypass searches, and git diff check passed.

### Limitations

none

## 2026-09-04 02:32 — Stabilize Quercus Marker and llama.cpp lifecycle

- Category: bugfix
- Area: Quercus processing

### Summary

Moved llama.cpp ownership to a pass-scoped backend controller. One keep-alive Surya server is started through the installed Marker environment, its validated loopback URL is supplied to every Marker conversion, and its exact process tree is stopped without Windows console-control broadcasts. Sync and processing shutdown now close the owned inference server, with focused tests and documentation updated.

### Limitations

Live ECO101 processing was not started during implementation; the user will validate the native Windows process behavior during the next backfill.
