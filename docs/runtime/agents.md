# Persistent agents

Act, Observer, and Assistant are built-in conversational agents backed by resumable Codex App Server threads. They are independent of generated function, service, and web-app skills and cannot be installed as skills. The Agents page exposes descriptions, effective policies, model/reasoning overrides, sessions, and Assistant proposals and assessments. Project planning remains separate.

## Workspace and authority

All three agents use `runtime/act/`. Role-specific developer instructions are supplied per thread; no shared managed instruction file is generated there. Act can write workspace files and explicit, user-approved memory. Observer and Assistant have read-only filesystem access. The backend owns synchronized knowledge and `knowledge/assistant/plans/`.

Named Codex permission profiles grant minimal runtime reads plus the managed root, and only Act's memory/workspace write roots. Command networking is disabled. The backend disables inherited MCP servers, plugins, apps, and delegation features for managed agent processes. An unsupported permission profile fails the turn instead of falling back to a broader sandbox. These are local single-user boundaries, not protection against a privileged host user or compromised backend.

Each agent has a default semantic read-only filter, maximum risk, explicit allowed function IDs, and explicit banned IDs. Bans win; explicit allows override the default semantic filter and risk limit. Integration read-only decisions use canonical registry effects, never MCP presentation hints. Availability, integration authorization, generated-function runtime permissions, and per-invocation approvals remain mandatory. Observer and Assistant default to read-only operations at any risk; generated functions remain write-capable by default. Act permits all available agent-callable functions. Observer has no web search; Act and Assistant have live Codex search. Filesystem and search permissions are fixed per role in this milestone; editable function overrides do not change them.

## Authenticated MCP and durable turns

The public host MCP registration remains separate. Managed threads start with a required private STDIO MCP registration and an opaque credential bound to the agent and Eidolon session. Only credential hashes are stored in the application database. The credential is passed to the trusted MCP child, not ordinary shell processes or readable workspace files. Discovery filters the catalog and every invocation rechecks current identity and policy. Audit records include agent/session identity and the active turn when established, without arguments or credentials. Deferred function approvals retain agent attribution and recheck current policy at dispatch.

Sessions are saved before Codex starts; their first queued turn creates the Codex thread. One dispatcher per agent serializes its turns while unrelated agents remain independent. Telegram and WeCom are transport-only adapters over this same session infrastructure: Telegram resolves `(chat_id, message_thread_id)` and WeCom resolves one persisted current session per user. The core session service has no cross-channel default session or stale-pointer fallback. Telegram topic mappings and WeCom current-session pointers are maintained by their adapters, while archiving and retention leave no routable channel binding. Turn rows snapshot the delivery provider, connection, chat/user, and Telegram thread so channel changes cannot redirect replies. Model routing is also independent: Act uses `act`, Observer uses `observer`, and Assistant uses `assessment` in the persisted Codex routing settings. Managed App Server processes restart at turn boundaries to refresh tool discovery and configuration, while saved Codex threads retain conversation context. Credentials are revoked after turns, archiving, and retention. Existing Act session/thread IDs are preserved, and the `/act` API remains an Act-only surface. Cancellation, archive, restart interruption, and missing-rollout recovery retain Act's existing behavior.

## Assistant proposals

Assistant receives a concise catalog of Act's current effective callable capabilities in its role-specific developer instructions. This catalog is descriptive and grants Assistant no additional tools. Its instructions direct it to act proactively, gather only useful additional context from Eidolon functions, read-only workspace files (notably `knowledge/assistant`), and internet search, and infer intent from evidence before suggesting concrete work. It must state material assumptions, ask only critical clarifications, and prefer high-value, timely, specific interventions over generic productivity suggestions. It reads proposal history before suggesting work. Assessment turns use the short trigger `Do an assessment now.` and rely on those persistent Assistant instructions for the assessment behavior.

Each Assistant thread may create at most five new proposals across all turns. Instructions state the limit and the backend atomically enforces a persistent session counter. Exact duplicate submissions consume no additional slot. Replacements with a valid predecessor and material-change explanation are unlimited and consume no slot. History cleanup does not restore slots. Existing sessions are initialized from their retained non-replacement proposals on upgrade.

The private `plan_approval_request` tool accepts `title`, `rationale`, `actions`, `instruction`, optional `references`, and paired optional `replaces_proposal_id`/`material_change`. It exists only for authenticated Assistant sessions and is absent from public MCP, PM, and generated-skill catalogs. Exact normalized instruction/reference duplicates return the existing proposal. Avoiding semantically similar plans is instruction-driven; a materially changed replacement references its predecessor and explains the change.

The database owns proposals; knowledge JSON files are regenerable projections. Local and Telegram decisions use the same atomic claim. Approval creates one new Act session and queued instruction in the same database transaction. Existing function approvals still apply. Execution state is tracked independently and never replayed after an interrupted live turn. Pending proposals remain locally available when Telegram is disconnected; the notification/approval bot delivers them when connected.

## Assistant retention

At most five Assistant sessions are retained across web, Telegram, and assessments, ordered by creation time and ID. Before a sixth is created, the oldest is removed with its turns and activity, its credential is revoked, any foreign-keyed Telegram topic mapping is removed, and its Codex thread is archived. Archived Assistant sessions count toward the five. Busy oldest sessions block creation; they are never deleted while queued/running. A SQLite write transaction serializes retention and creation across callers. Startup and completed turns reconcile excess records using the same rule.

Proposal records, approvals, knowledge, shared files, and linked Act executions survive session pruning. Source session IDs remain historical attribution. Codex rollout files are archived rather than physically purged.

Before each manual or scheduled assessment, the backend checks proposal references formatted as `todo:<Notion page ID>` or `goal:<Atlas goal ID>`. If any attached item is missing, a Todo is done, or an Atlas Goal has 100% progress, it deletes the proposal record and its knowledge JSON. Linked Act sessions are preserved. Todo pagination must finish before absence is treated as deletion. Unavailable sources and legacy/unrecognized references are retained. Old Telegram approval buttons cannot approve a deleted proposal. This cleanup is permanent and applies to every proposal status.

## API

- `GET /agents`, `GET /agents/{agentId}`, `PUT /agents/{agentId}/policy`.
- `GET|POST /agents/{agentId}/sessions`, `GET|DELETE /agents/{agentId}/sessions/{sessionId}`.
- `POST /agents/{agentId}/sessions/{sessionId}/turns` and `POST .../turns/{turnId}/cancel`.
- `GET /agents/assistant/proposals`, `POST /agents/assistant/proposals/{id}/approve|deny`.
- `GET|PUT /agents/assistant/assessment`, `POST /agents/assistant/assessment/run`.

The assessment service starts disabled. Enabling establishes a durable first occurrence 72 hours later. Each assessment uses a fresh Assistant session; failed/blocked claimed occurrences are consumed without retry. See [scheduling](scheduling.md) and [Telegram](../integrations/telegram.md).
