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
