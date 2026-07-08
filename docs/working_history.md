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
