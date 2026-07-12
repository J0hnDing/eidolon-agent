# Working History

This file is reserved for future working-history entries.

Do not backfill old project history here. The file starts empty by design so future entries can be explicit, timestamped, and attributable to actual changes made after this documentation system was introduced.

## Purpose

Use this file to record future notable changes with:

- timestamp,
- changed area,
- intention,
- summary of files or behavior changed,
- verification performed,
- known follow-up or limitations.

## Entry Format

Use this template for future entries:

```markdown
## YYYY-MM-DD HH:MM TZ - Short Title

- Area:
- Intention:
- Changed:
- Verification:
- Follow-up:
```

## Current Entries

## 2026-07-12 01:39 America/Toronto - Project Build Context Optimization And GitHub E2E

- Area: ProductManager, Builder, and Tester workflow context; generated-skill permissions and runtime budgets; failed-build handling; agent ownership boundaries; workflow documentation and tests.
- Intention: Reduce redundant Codex context and token consumption while preserving explicit permission bounds, deterministic package validation, generated-code quality, and separate approval boundaries for generation, installation, runtime permissions, scheduling, and execution.
- Changed: Compacted downstream ProductManager intent and permission inputs; limited Builder to the current task, selected backend APIs, direct-parent contracts, explicit permission bounds, and workspace paths; limited node and final Tester inputs to compact contracts and workspace paths; normalized and truncated repair failure context while preserving the current interface contract; deterministically assigned omitted required package files to a root DAG task; enforced Tester ownership by restoring Python test files changed by Builder and failing the Builder invocation; allowed one focused Tester pytest self-check; increased generated skill runtime timeout to 120 seconds while retaining a 45-second backend Codex caller timeout; documented model-per-task/chat selection and Codex CLI/model-version compatibility as TODOs. The generated GitHub skill was corrected after a live-page audit so duplicate Trending cards are normalized and entries beyond the requested limit do not create false partial failures. Post-work decision: the user reverted optimization 1, which skipped intent refinement for clear first-turn Project requests, and optimization 8, which resumed a failed build directly from its current DAG task.
- Verification: Before those two reverts, an E2E run with the unchanged GitHub Trending prompt completed successfully at 382,608 tokens versus the 684,621-token baseline, saving 302,013 tokens (44.1%); Builder usage fell 51.2%, node Tester usage fell 53.7%, and final Tester usage fell 30.7%. The corrected proposed skill passed platform validation and 24 tests, a live GitHub Trending parser smoke check returned 10 unique projects without false failures, the full backend suite passed with 205 tests, and `git diff --check` passed.
- Follow-up: Re-measure end-to-end token usage after the two reverts before treating 44.1% as representative of the current workflow. Model routing by DAG task and chat, plus Codex CLI/model-version compatibility, remain unimplemented TODOs. The generated skill remains proposed, disabled, uninstalled, unscheduled, and subject to separate pending runtime permission approval.

## 2026-07-11 02:38 America/Toronto - Codex Build Usage Tracking

- Area: Codex integration, agent-run persistence, DAG workflow controls, skill and settings UI, documentation.
- Intention: Distinguish per-invocation build token consumption from account allowance remaining, while keeping a 5% reserve in both Codex allowance windows before admitting more DAG work.
- Changed: Added adapter/model-aware ProductManager, Builder, and Tester invocation token records with per-step and per-build totals; displayed token usage in DAG nodes, agent-run details, and completed skill builds; added a persistent local Codex App Server client and `/usage/codex` endpoint for 5-hour and weekly allowance windows; added Settings usage UI; added ready-node execution batches plus allowance-based DAG pause below 5% in either window and resume from persisted completed nodes; explicitly excluded skill runtime from build accounting; added `docs/todo.md` with the follow-up to implement skill runtime token tracking.
- Verification: Full backend suite passed with a fresh system `--basetemp`, `200 passed`; focused usage and DAG workflow tests passed, `36 passed`; frontend TypeScript and Vite production build passed with `npm run build`; a live local App Server probe returned both normalized allowance windows; `git diff --check` passed.
- Follow-up: Implement skill runtime token tracking as recorded in `docs/todo.md`.

## 2026-07-09 00:50 America/Toronto - ProductManager Build Instruction Split

- Area: ProductManager build workflow, permission-plan schema, agent instructions, backend permission parsing, docs, tests.
- Intention: Make blueprint and permissions one ProductManager action, keep task DAG planning as a separate post-approval phase, and use a flat runtime permission syntax where domains live in `runtime.network`.
- Changed: Added active `product_manager/blueprint_and_permissions.md` and lowercase `product_manager/task_dag.md`, left `product_manager/build.md` as legacy, removed PM summary Codex calls in favor of fallback summaries, removed `approval_summary`, removed unsupported DAG-phase block/ask language, changed `permissions.json` to use flat `runtime.network`/filesystem/codex fields instead of `runtime.permissions` plus `runtime.network_domains`, and updated `CodexService`, `AgentWorkflowService`, `PermissionService`, tests, and docs to parse and preserve that shape.
- Verification: Focused PM/schema tests passed with `.\.venv\Scripts\python.exe -m pytest --basetemp "$env:TEMP\pa-pytest-codex-pm-schema" ...` from the repo root; full backend suite passed with `.\.venv\Scripts\python.exe -m pytest --basetemp "$env:TEMP\pa-pytest-codex-full-schema" backend\tests` from the repo root, `190 passed`.
- Follow-up: Existing DB/API fields named `requested_network_domains_json` remain for compatibility and are populated from `permission_plan.runtime.network`; unrelated generated files under `skills/proposed/weekly_github_trend_analyzer/` remain untracked.

## 2026-07-08 15:06 America/Toronto - Remove Hybrid Skill Type

- Area: Skill schema, Project Build planning, frontend skill UI, docs.
- Intention: Collapse executable skills into `automation` only while allowing automation packages to optionally include `SKILL.md`.
- Changed: Removed `hybrid` from shared skill type literals, manifest validation, planner prompts, generated-agent instructions, tool filtering, frontend type choices, and docs. Automation still requires `entrypoint` and tests; `instructions_path` is optional for automation and validated when present.
- Verification: `..\.venv\Scripts\python.exe -m pytest tests\test_manifest_validator.py tests\test_proposed_skill_service.py tests\test_chat_generation.py tests\test_scheduler_service.py tests\test_tools.py --basetemp C:\Users\John\personal-agent\runtime\pytest-no-hybrid-focused` passed from `backend`; `..\.venv\Scripts\python.exe -m pytest tests\test_agent_workflow_service.py tests\test_skill_version_service.py tests\test_skill_runner.py tests\test_docker_skill_runner.py --basetemp C:\Users\John\personal-agent\runtime\pytest-no-hybrid-workflow` passed from `backend`; `npm run build` passed from `frontend`.
- Follow-up: Existing persisted records or generated manifests with `skill_type = "hybrid"` will now fail validation until migrated or regenerated as `automation`.

## 2026-07-08 02:34 America/Toronto - Manifest Schedule Registration

- Area: Project Build planning, backend API catalog, install-time scheduling.
- Intention: Make schedule intent ProductManager-owned manifest metadata instead of a Builder backend API, and register manifest schedules when skills are installed.
- Changed: Removed the PM-visible Scheduling API catalog entry, preserved schedule intent from plan/blueprint into `manifest.json`, registered manifest-declared schedules as pending records during install, and improved fake planning names for GitHub trending weekly skills.
- Verification: Focused schedule/planning tests passed, then full backend suite passed with `..\.venv\Scripts\python.exe -m pytest --basetemp C:\Users\John\personal-agent\runtime\pytest-full-schedule-change` from `backend`.
- Follow-up: Existing proposed skills generated before this change are not renamed in place.

## 2026-07-08 01:52 America/Toronto - Skill Codex API And DAG API Context

- Area: Runtime permissions, backend skill APIs, Project Build DAG workflow, Agent Runs UI.
- Intention: Allow skills to ask the backend to call Codex without granting shell access, and give Builder only the backend API context selected by ProductManager task nodes.
- Changed: Added manifest `permissions.codex`, `POST /skills/{skill_id}/codex`, backend API catalog ids for Codex and Tool UI schema, task-node `backend_api_ids`, Builder `backend_api_context`, runner backend URL env vars, and an explicit DAG view in Agent Run detail.
- Verification: Full backend suite passed with `..\.venv\Scripts\python.exe -m pytest --basetemp C:\Users\John\personal-agent\runtime\pytest-full-final-codex-api` from `backend`; frontend build passed with `npm run build` from `frontend`; Chrome UI E2E submitted the requested Project prompt, approved generation, and verified Agent Run #11 showed the explicit task DAG with `core_skill` status `done`.
- Follow-up: Future backend API candidates inferred but not added: runtime cache helper API, runtime permission status API, skill run metadata API, and memory lookup API.

## 2026-07-07 21:43 America/Toronto - Backend Manifest Skeletons

- Area: Project-mode DAG build workflow, manifest validation, Builder/ProductManager instructions.
- Intention: Stop Builder from inventing manifest shape from prose while preserving the DAG workflow.
- Changed: Backend now seeds `manifest.json` from `blueprint.json` and `permissions.json`, fills missing manifest fields after Builder steps, validates declared executable entrypoint files, reports backend-seeded manifest paths as updated in fallback interface artifacts, and tells ProductManager to avoid over-splitting tightly coupled same-file implementation nodes.
- Verification: Targeted backend workflow suite passed with `..\.venv\Scripts\python.exe -m pytest tests\test_agent_workflow_service.py --basetemp C:\Users\John\personal-agent\runtime\pytest-agent-workflow` from `backend`.
- Follow-up: None known.

## 2026-07-07 02:32 America/Toronto - DAG Build Workflow

- Area: Project-mode skill build workflow, Codex service adapters, agent instructions, agent-run UI.
- Intention: Replace the linear milestone build flow with the documented DAG-based task-node workflow.
- Changed: Split ProductManager build actions into intent refinement, plausibility review, blueprint, permissions, and post-approval task DAG creation; added backend DAG validation, task-node artifacts, interface artifacts, node-specific tests, final E2E tests, task retry aliases, and task-node UI labels.
- Verification: Full backend suite passed with `..\.venv\Scripts\python.exe -m pytest --basetemp <temp>` from `backend`; frontend build passed with `npm run build` from `frontend`.
- Follow-up: None known.

## 2026-07-04 13:09 America/Toronto - Two-Phase ProductManager Build Review

- Area: ProductManager build workflow, chat Project mode, agent instructions, frontend chat state.
- Intention: Ensure ProductManager reviews intent and plausibility before blueprint/permission artifacts are created, while allowing unclear requests to continue in the same chat.
- Changed: Added PM build review before artifact creation, a separate `product_manager_plausibility_review.md` instruction file, `needs_input` generation requests, same-chat clarification continuation, tool UI milestone guidance, and chat UI pending request tracking.
- Verification: `..\.venv\Scripts\python.exe -m pytest` from `backend`; `npm run build` from `frontend` with the documented Vite/esbuild sandbox escalation.
- Follow-up: Consider a durable backend conversation table if multi-device chat continuity becomes a requirement.
