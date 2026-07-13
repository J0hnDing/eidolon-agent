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

## 2026-07-12 20:50 - Modular Project Build Workflows

- Summary: Modular project build workflows. Added ProductManager selection of a single backend-only `build_workflow` value, persisted it separately from `blueprint.json`, kept intent refinement, plausibility review, blueprint creation, permission planning, and build-time approval as the shared starting sequence, and routed post-approval execution through a trusted workflow registry. Colocated shared preflight, DAG, and single-Codex Markdown instructions and prompt composition with their owning workflow packages; `CodexService` now delegates project-build prompt construction while retaining shared invocation, parsing, routing, usage, workspace, and safety primitives. Moved existing DAG execution, pause/resume, and retry orchestration into the `task_dag` package, and added a `single_codex` package that gives Codex the approved blueprint and effective permissions for one planning, build, and test invocation. The workflow/chat regression suite passed with 85 tests, the final single-Codex end-to-end test passed and executed the generated JSON entrypoint, the frontend production build passed, and `git diff --check` passed.
- Limitations/Future implementations: Independent backend final package, manifest, declared-file, acceptance-criteria, and test validation after `single_codex` completion remains deferred in `docs/todo.md`. Runtime permission review remains enforced from the generated manifest.

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

## 2026-07-08 15:06 - Remove Hybrid Skill Type

- Summary: Removed the hybrid skill type. Restricted executable skills to `automation`, retained optional `SKILL.md` support for automation packages, and updated shared schemas, manifest validation, prompts, agent instructions, tool filtering, frontend choices, and documentation. Focused backend suites and the frontend build passed.
- Limitations/Future implementations: Persisted records or generated manifests using `skill_type = "hybrid"` require migration or regeneration as `automation`.

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
