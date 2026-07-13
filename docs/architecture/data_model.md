# Data Model

The backend uses SQLite through SQLAlchemy models in `backend/app/models/entities.py`. The application engine enables SQLite foreign-key enforcement on every connection. The database is local development state; schema compatibility is handled in `backend/app/db.py` with lightweight local schema updates rather than a full migration system.

## Core Tables

### messages

Compatibility storage for chat messages with role, content, optional conversation id, and creation time. Current chat history is frontend-local; the backend table remains only for older rows and memory-source cleanup. Deleting a frontend conversation asks the backend to remove matching compatibility rows and clear memory source links. The long-term ownership decision is tracked in `docs/todo.md`.

### codex_routing_settings

Stores the single-user Codex invocation routing document. It contains independent Chat settings, ProductManager action settings, Builder default/difficulty/repair/update settings, and Tester task/final/update settings. Model ids and reasoning efforts are user-owned settings; ProductManager task DAG output does not contain them.

### memory_facts

Stores explicit user-editable memory facts. Typical categories include interests, goals, preferences, routines, trusted sources, blocked sources, writing style, and risk tolerance.

### skills

The central skill record. Important fields:

- `name`
- `description`
- `skill_type`: `instruction` or `automation`
- `interface_type`: `chat`, `tool`, or `hidden`
- `status`: `building`, `proposed`, `installed`, `failed`, or legacy tombstone `deleted`
- `risk_level`
- manifest/instructions/installed paths
- input/output/tool UI schemas
- `active_version_id`
- `enabled`

Enabled state is represented only by `enabled`; `disabled` is not a lifecycle status. Local schema migration converts legacy `status = disabled` rows to `status = installed, enabled = false`.

### skill_versions

Tracks versioned installed skill folders. Active installed skills point to an active version folder. Updates create draft/proposed versions and do not mutate the active folder in place.

### skill_runs

Stores manual, tool, or scheduled run results:

- input/output JSON
- stdout/stderr
- exit code
- start/end timestamps
- status and error message
- ordered runtime Codex invocation records with adapter/model identity and token breakdowns
- aggregate input, cached-input, output, reasoning-output, and total token counts

### skill_operation_locks

Backend-enforced local locks for per-skill operation safety. These prevent overlapping run/install/update/repair/delete operations on the same skill.

### skill_schedules

Stores schedule definitions for installed executable skills. Canonical statuses are `pending`, `active`, `paused`, and `denied`. Deletion removes the row; it is not a persisted schedule status.

### approval_requests

Stores build-time, runtime, schedule, and update approval requests. The approval request is the durable record of what was requested, why, risk level, and user decision.

### skill_generation_requests

Stores Project-mode skill generation requests and the initial/updated generation plan.

### agent_runs and agent_run_steps

Store bounded agent workflows and role-specific steps. Agent communication is persisted as structured artifacts rather than free-form hidden agent chat. New build runs persist backend-only `build_workflow` separately from `blueprint_json` so routing state does not leak into downstream product artifacts.

DAG build runs should persist:

- the refined intent prompt;
- plausibility `decision_json`;
- `blueprint_json`;
- `permission_plan_json`;
- `build_workflow` (`single_codex` or `task_dag`);
- `task_dag_json`;
- canonical `current_task_id`;
- per-task failure counts;
- final end-to-end failure count.

Agent run steps identify the explicit action and canonical `task_node_id` when applicable. Local schema migration copies values from the former physical columns into these canonical columns; the API exposes only task-node terminology.

Agent runs also persist aggregate build-token fields and an optional usage pause reason. Each step persists an ordered `codex_invocations_json` list with action, adapter, requested/effective model, requested/effective reasoning effort, route source, task difficulty when applicable, and token breakdown plus aggregate token columns. These fields describe build-agent activity only. Runtime Codex calls are recorded separately on the active `skill_runs` row and do not contribute to build totals.

## Status Values

Skill statuses:

```text
building, proposed, installed, failed, deleted
```

Version statuses:

```text
active, draft, proposed_update, archived, discarded
```

Run statuses:

```text
pending, running, succeeded, failed, blocked
```

Approval statuses:

```text
pending, approved, denied, expired, superseded
```

Schedule statuses:

```text
pending, active, paused, denied
```

Agent run statuses:

```text
pending, running, waiting_for_approval, paused, succeeded, failed, cancelled, blocked
```

DAG task node statuses:

```text
pending, ready, building, testing, fixing, done, failed, blocked
```
