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
- `runtime`: `function` or `web_app`
- `status`: `building`, `proposed`, `installed`, `failed`, or legacy tombstone `deleted`
- `risk_level`
- manifest/instructions/installed paths
- input/output schemas
- declared function requirements
- `active_version_id`
- `enabled`

Enabled state is represented only by `enabled`; `disabled` is not a lifecycle status. Local schema migration converts legacy `status = disabled` rows to `status = installed, enabled = false`. Existing rows created before the runtime split migrate to `runtime = function`; filesystem synchronization refreshes installed records from the active manifest.

### skill_versions

Tracks versioned installed skill folders. Active installed skills point to an active version folder. Updates create draft/proposed versions and do not mutate the active folder in place.

### skill_runs

Stores manual or scheduled function-run results:

- input/output JSON
- stdout/stderr
- exit code
- start/end timestamps
- status and error message
- ordered runtime Codex invocation records with success/failure status, adapter/model identity, CLI diagnostics, and token breakdowns
- aggregate input, cached-input, output, reasoning-output, and total token counts
- target active version and invocation source
- caller skill/version for cross-skill calls
- schedule id or web-application instance attribution when applicable
- a hash of the ephemeral caller capability while the run is active

`skill_runs` is intentionally bounded and is not used to represent a persistent web server.

### function_access_approvals

Links one caller skill, one target function, the user-facing approval request, and the backend fingerprint of the approved target callable contract. Historical rows are invalidated rather than silently reused when target risk, permissions, dependencies, or input/output schemas change. Low-risk relationships do not need rows.

### web_app_instances

Stores persistent version-pinned service instances separately from bounded runs. It includes skill/version ids, startup/readiness/access/stop timestamps, lifecycle status, runner mode, loopback upstream, application and optional trusted-relay Docker identities or a local process identity, a hash of the scoped capability token, and bounded logs/error diagnostics.

### web_app_sessions

Stores distinct browser/application sessions attached to an instance. It includes active/closed/expired status, a hash of the opaque hostname bearer, a non-secret gateway-origin identity, access/expiry timestamps, and closure time. The full bearer hostname is returned once and is not persisted.

### web_app_audit_records

Stores capped lifecycle, gateway-interaction, and privileged-operation metadata. Request/response bodies and Codex prompt/response contents are not stored in this table.

### skill_operation_locks

Backend-enforced local locks for per-skill operation safety. These prevent overlapping run/install/update/repair/delete operations on the same skill.

### skill_schedules

Stores schedule definitions for installed skills. Canonical statuses are `pending`, `active`, `paused`, and `denied`. Deletion removes the row; it is not a persisted schedule status.

### approval_requests

Stores build-time, runtime, schedule, update, and caller-target function-access approval requests. The approval request is the durable record of what was requested, why, risk level, and user decision.

### skill_generation_requests

Stores Project-mode skill generation requests and the initial/updated generation plan.

### agent_runs and agent_run_steps

Store bounded workflows with explicit ProductManager, Builder, Tester, and backend steps. Every Codex-backed step stores the exact prompt string supplied to the adapter and the exact final response string returned by it, separately from backend-normalized structured workflow artifacts. Backend steps store an action and fixed summary only; they do not expose agent input/output fields. New build runs persist backend-only `build_workflow` separately from `blueprint_json` so routing state does not leak into downstream product artifacts.

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

Agent run steps identify the explicit action and canonical `task_node_id` when applicable. Permission-review steps link their approval request through a dedicated column instead of hiding the id in input/output JSON. Local schema migration copies values from the former physical columns into these canonical columns; the API exposes only task-node terminology.

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
pending, running, succeeded, partial, failed, blocked
```

Web application instance statuses:

```text
starting, ready, healthy, unhealthy, stopped, failed
```

Web application session statuses:

```text
active, closed, expired
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
