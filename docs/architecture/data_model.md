# Data Model

The backend uses SQLite through SQLAlchemy models in `backend/app/models/entities.py`. The application engine enables SQLite foreign-key enforcement on every connection. The database is local development state; schema compatibility is handled in `backend/app/db.py` with lightweight local schema updates rather than a full migration system.

## Core Tables

### messages

Compatibility storage for chat messages with role, content, optional conversation id, and creation time. Current chat history is frontend-local; the backend table remains only for older rows and memory-source cleanup. Deleting a frontend conversation asks the backend to remove matching compatibility rows and clear memory source links. The long-term ownership decision is tracked in `docs/todo.md`.

### codex_routing_settings

Stores the single-user Codex invocation routing document. It contains independent Act, Observer, and Assistant assessment settings, ProductManager action settings, Builder default/difficulty/repair/update settings, and Tester task/final/update settings. Model ids and reasoning efforts are user-owned settings; ProductManager task DAG output does not contain them. Legacy stored `chat` routing values are ignored.

### codex_mcp_settings

Stores the single host registration state: whether running MCP processes may invoke tools, the fingerprint of the exact Eidolon-owned `mcp_servers.eidolon` table, bounded last-error metadata, and update time. Removal commits `enabled = false` before editing Codex configuration so already-running MCP processes are revoked immediately.

### act_sessions and act_turns

`act_sessions` stores each durable Codex thread id, title, origin, lifecycle status, and timestamps. `act_turns` stores the user message, queued/running/terminal state, execution start/completion timestamps, live Codex turn id, concise activity, final answer or bounded failure, cancellation request, and optional transport-neutral delivery provider/target/status. Only queued turns are safe to retain across restart; running rows are recovered as interrupted.

### act_telegram_bindings

Stores each conversational Telegram bot connection's selected canonical session pointer. A normal message repairs a null, missing, wrong-agent, or archived pointer using the newest active session for that agent, or a new session when none exists. Archiving or deleting the referenced session clears the pointer. Disconnect deletes the binding while preserving sessions and shared files.

### memory_facts

Stores explicit user-editable memory facts. Typical categories include interests, goals, preferences, routines, trusted sources, blocked sources, writing style, and risk tolerance.

### skills

The central skill record. Important fields:

- `name`
- `description`
- `runtime`: `function`, `service`, or `web_app`
- `status`: `building`, `proposed`, `installed`, `failed`, or legacy tombstone `deleted`
- `risk_level`
- manifest/instructions/installed paths
- input/output schemas
- declared function requirements
- normalized integration requirements derived from the actual active manifest
- `active_version_id`
- `enabled`

Enabled state is represented only by `enabled`; `disabled` is not a lifecycle status. For services this field is canonical and the required schedule's active/paused status is a compatibility projection. Local schema migration converts legacy `status = disabled` rows to `status = installed, enabled = false`. Existing rows created before the runtime split migrate to `runtime = function`; filesystem synchronization refreshes installed records from the active manifest. Active manifests form a directed function graph used to derive transitive risk, permission review, and availability.

### skill_versions

Tracks versioned installed skill folders. Active installed skills point to an active version folder.

### skill_runs

Stores bounded function or service-run results:

- input/output JSON
- stdout/stderr
- exit code
- start/end timestamps
- status and error message
- ordered runtime Codex invocation records with success/failure status, adapter/model identity, CLI diagnostics, and token breakdowns
- aggregate input, cached-input, output, reasoning-output, and total token counts
- target active version and invocation source
- caller skill/version and parent run id for cross-skill call chains
- schedule id or web-application instance attribution when applicable
- intended schedule time, trigger reason, and deterministic schedule-occurrence idempotency key
- a hash of the ephemeral caller capability while the run is active

`skill_runs` is intentionally bounded and is not used to represent a persistent web server.

### function_access_approvals

Links one caller skill, one target function, the user-facing approval request, and the backend fingerprint of the approved target callable contract. Historical rows are invalidated rather than silently reused when target risk, permissions, dependencies, or input/output schemas change. Low-risk relationships do not need rows.

### integration_connections

Stores sanitized connection rows. A provider may have multiple rows, but a partial unique index permits at most one `is_default` row per provider; current Settings/OAuth flows read and replace that default without exposing a public account selector. GitHub and Quercus store validated account identity. Notion additionally stores sanitized bot/workspace identity plus separate non-secret configured Todo and Reports data-source IDs under one credential reference. The Reports ID is nullable for compatibility with legacy Todo-only connections. Atlas may use the row for its optional owned-process passphrase lifecycle. Gmail and Outlook store only OS-secret-store implementation ids and opaque refresh-token references, with Outlook's account id hashed before persistence. Existing connection ids, secret references, grants, approvals, and dependent foreign keys survive the in-place uniqueness migration; legacy rows become defaults by provider.

### quercus_courses, quercus_course_exclusions, and quercus_sync_resources

`quercus_courses` stores selected or retained course identity, stable title-based local path, owning Canvas account, course presentation fields, sync generation/status, skipped-file counts, and independent processing status and success/failure counts. Deselection stops updates without deleting the retained mirror. Connecting a different account is rejected while any retained course rows remain.

`quercus_course_exclusions` stores only the Canvas account and course identifiers needed to keep explicitly deleted tracked or untracked courses out of later provider listings. Exclusions are account-scoped and do not count as retained local copies.

`quercus_sync_resources` owns every remote Canvas resource identity and parent, stable collision-resolved raw and processed relative paths, remote version/update fields, normalized source/processing fingerprints, size/type, download and processing state, processor/error/completion fields, and last-seen generation. `quercus_processing_settings` is the singleton global processing-method selection and user-selected llama.cpp installation directory. This bookkeeping remains in SQLite; synchronized knowledge directories contain readable course material only, with no manifests, sidecars, frontmatter, or ID-bearing filenames.

Calendar and Gmail use separate connection rows, account identities, OAuth refresh-token references, and service secret namespaces. They may authorize different Google accounts. Outlook has its own default connection and Microsoft refresh-token namespace. Singleton `google_oauth_client_configs` and `microsoft_oauth_client_configs` rows reference OAuth application configuration in the OS secret store; neither contains a service grant or account identity. Invocation approvals and integration audit rows retain the selected provider, internal connection id, and account id so approval replay and audit attribution become stale when the default connection changes.

### telegram_bot_connections

Stores role/default selection for the independent notification/approval and Act bots, sanitized bot identity and status, paired private chat/user ids, persisted update offset, and hashed one-time pairing state. Bot tokens remain in the operating-system secret store.

### wecom_observer_bindings, wecom_observer_user_bindings, and wecom_inbound_messages

`wecom_observer_bindings` stores the singleton WeCom connection's hashed short-lived pairing code and expiry. `wecom_observer_user_bindings` stores one row per paired private user and that user's selected canonical Observer session pointer. `wecom_inbound_messages` stores per-connection message IDs for duplicate suppression. WeCom Bot ID and connection metadata use the generic `integration_connections` row; the Bot Secret remains only in the operating-system secret store. User removal deletes only one binding. Disconnect removes pairing, user-binding, and deduplication rows without deleting Observer sessions. Session archive or retention deletion clears every referencing Telegram or WeCom pointer.

### invocation_approvals

Stores the immutable target/version-or-contract/account identity, caller attribution, bounded business input and hash, reason, Telegram delivery metadata, separate decision/execution states, outcome, errors, and timestamps for per-call approval. It is intentionally unrelated to `approval_requests`.

### integration_authorizations

Links one skill/provider integration-contract fingerprint to its `integration_access` approval. Different fingerprints may coexist so a proposed draft cannot revoke the active version's unchanged contract; validated account identity changes invalidate all existing rows while preserving history.

### integration_audit_records

Stores one bounded sanitized record per authenticated caller attempt: caller skill/version, function run or web-app instance, operation, normalized repository resource when applicable, timing, status, normalized error type, and size metadata. Raw inputs, credentials, secret references, headers, capability material, and provider responses are excluded.

### mcp_audit_records

Stores one bounded sanitized record per Codex MCP attempt: fixed caller type, exact catalog function ID and category, normalized resource identifier when applicable, status, normalized error type, request/response byte counts, and timestamps. Prompts, arguments, outputs, credentials, headers, secret references, and capability material are excluded.

### web_app_instances

Stores persistent version-pinned service instances separately from bounded runs. It includes skill/version ids, startup/readiness/access/stop timestamps, lifecycle status, runner mode, loopback upstream, application and optional trusted-relay Docker identities or a local process identity, a hash of the scoped capability token, and bounded logs/error diagnostics.

### web_app_sessions

Stores distinct browser/application sessions attached to an instance. It includes active/closed/expired status, a hash of the opaque hostname bearer, a non-secret gateway-origin identity, access/expiry timestamps, and closure time. The full bearer hostname is returned once and is not persisted.

### web_app_audit_records

Stores capped lifecycle, gateway-interaction, and privileged-operation metadata. Request/response bodies and Codex prompt/response contents are not stored in this table.

### skill_operation_locks

Backend-enforced local locks for per-skill operation safety. These prevent overlapping run/install/update/repair/delete operations on the same skill.

### skill_schedules

Stores exactly one schedule definition for each installed generated service. `skill_id` is unique. Canonical statuses are `active` and `paused`; installed services begin paused. Functions and web applications have no schedule rows. The manifest seeds initial state, while later edits remain backend runtime state across version activation.

### schedule_runtime_states and schedule_occurrences

`schedule_runtime_states` stores the current definition fingerprint, active-since boundary, and stable interval anchor for both generated-service and platform schedules. Its enabled flag and optional configuration JSON persist backend-owned platform service availability and schedule overrides; disabled schedules have no active boundary.

`schedule_occurrences` is the durable at-most-once ledger. Each row has one deterministic key for a schedule definition and intended UTC fire time, plus its trigger reason, status, timestamps, error, and optional resulting `skill_run`. The unique occurrence key is claimed before execution. Every terminal result consumes the occurrence; failed, blocked, partial, and interrupted occurrences are never retried.

### approval_requests

Stores build-time, runtime, update, caller-target function-access, and skill-specific integration-access approval requests. The approval request is the durable record of what was requested, why, risk level, and user decision. The nullable physical `schedule_id` column may remain in older local databases for compatibility, but service schedules do not use an approval request.

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

## Persistent agent state

`act_sessions.agent_id` attributes existing sessions to Act and new sessions to their immutable agent. `agent_policies` holds editable function policies/model overrides; `agent_credentials` holds revocable session credential hashes. `agent_proposals` stores approval and execution state independently of retained sessions. `assistant_assessment_state` holds the paused/enabled 72-hour anchor and next/last state; occurrences reuse `schedule_occurrences`. MCP audits carry agent/session/turn attribution without storing tool arguments.
