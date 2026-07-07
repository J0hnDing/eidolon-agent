# Data Model

The backend uses SQLite through SQLAlchemy models in `backend/app/models/entities.py`. The database is local development state; schema compatibility is handled in `backend/app/db.py` with lightweight local schema updates rather than a full migration system.

## Core Tables

### messages

Stores chat messages with role, content, optional conversation id, and creation time.

### memory_facts

Stores explicit user-editable memory facts. Typical categories include interests, goals, preferences, routines, trusted sources, blocked sources, writing style, and risk tolerance.

### skills

The central skill record. Important fields:

- `name`
- `description`
- `skill_type`: `instruction`, `automation`, or `hybrid`
- `interface_type`: `chat`, `tool`, or `hidden`
- `status`: `building`, `proposed`, `installed`, `disabled`, `failed`, or `deleted`
- `risk_level`
- manifest/instructions/installed paths
- input/output/tool UI schemas
- `active_version_id`
- `enabled`

### skill_versions

Tracks versioned installed skill folders. Active installed skills point to an active version folder. Updates create draft/proposed versions and do not mutate the active folder in place.

### skill_runs

Stores manual, tool, or scheduled run results:

- input/output JSON
- stdout/stderr
- exit code
- start/end timestamps
- status and error message

### skill_operation_locks

Backend-enforced local locks for per-skill operation safety. These prevent overlapping run/install/update/repair/delete operations on the same skill.

### skill_schedules

Stores approved or pending schedule definitions for installed executable skills.

### approval_requests

Stores build-time, runtime, schedule, and update approval requests. The approval request is the durable record of what was requested, why, risk level, and user decision.

### skill_generation_requests

Stores Project-mode skill generation requests and the initial/updated generation plan.

### agent_runs and agent_run_steps

Store bounded agent workflows and role-specific steps. Agent communication is persisted as structured artifacts rather than free-form hidden agent chat.

DAG build runs should persist:

- the refined intent prompt;
- plausibility `decision_json`;
- `blueprint_json`;
- `permission_plan_json`;
- `task_dag_json`;
- current task node id instead of current milestone;
- per-task failure counts;
- final end-to-end failure count.

Agent run steps should identify the explicit action being performed and the task node id when applicable. During migration from the linear milestone workflow, existing names such as `current_milestone` and `milestone_name` may be treated as compatibility aliases for `current_task_id` and `task_node_id`, but new code should use task-node terminology.

## Status Values

Skill statuses:

```text
building, proposed, installed, disabled, failed, deleted
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

Agent run statuses:

```text
pending, running, waiting_for_approval, succeeded, failed, cancelled, blocked
```

DAG task node statuses:

```text
pending, ready, building, testing, fixing, done, failed, blocked
```
