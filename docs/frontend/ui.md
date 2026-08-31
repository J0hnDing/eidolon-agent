# Frontend UI

The frontend is a React/Vite TypeScript app under `frontend/src`.

## Navigation

Routes are defined in `frontend/src/App.tsx`:

- `/chat`
- `/memory`
- `/skills`
- `/functions`
- `/apps`
- `/apps/:skillId`
- `/skills/:skillId`
- `/schedules`
- `/agent-runs`
- `/agent-runs/:agentRunId`
- `/approval-requests`
- `/settings/appearance`
- `/settings/usage`
- `/settings/project`
- `/settings/models`
- `/settings/integrations`
- `/settings/permissions`

The application shell groups these destinations into Workspace, Capabilities, and Control navigation. It uses one persistent desktop sidebar and a horizontally scrollable compact navigation bar on narrower screens. Each destination has a text label and decorative icon, the current route remains visibly selected, and a keyboard skip link moves directly to page content.

## Visual and Interaction System

The shared stylesheet defines Eidolon's dark-first control-plane interface across every route. Near-black graphite surfaces establish hierarchy, while the desaturated blue scale is reserved for hover, selection, focus, and a small number of high-priority actions. Plus Jakarta Sans is the normal interface face and IBM Plex Mono is reserved for technical metadata, code, function names, and timestamps. Spatial layout relies primarily on whitespace, dividers, and segmented rows rather than wrapping every group in a rounded card; containment boxes remain for approvals, modals, and sandboxed application chrome where the boundary carries meaning.

Page headers begin with the title and retain their descriptive copy beneath it. They do not add a second eyebrow-style tagline above the title.

Passive status presentation uses a small semantic dot with a plain-text label instead of a colored capsule. Color is never the only state signal. Page and message transitions use short motion with no workflow-level delay. `prefers-reduced-motion` disables non-essential animation, and focused controls retain an explicit visible ring.

Settings section navigation stays visible while scrolling and becomes horizontally scrollable on narrow screens. Chat keeps conversation management separate from the transcript, uses a bounded scrolling message area, and anchors its composer at the bottom of the chat surface. Approval Requests uses a selectable request browser with visible active, status, and risk states beside the detail panel.

## API Client

`frontend/src/api/client.ts` defines API types and request helpers. Frontend types mirror backend schemas for skills, runs, versions, approvals, schedules, generation requests, and agent runs.

## Chat Page

The shared conversation page supports two explicit, conversation-owned modes:

- `project`: writes the initial message unchanged through the one-time intent placeholder, then starts a resumable `pm_plan_build` session for a reusable skill request. The placeholder does not call Codex. The session may ask a clarification question or reject the request before any blueprint, permission, or task DAG artifacts are created; the next reply in the same chat resumes the same generation request and Codex thread.
- `act`: binds the local conversation to a backend-owned durable Act session and renders its queued/running/terminal turns in the shared transcript.

Conversation mode is fixed after creation. The sidebar provides separate New Project and New Act actions, and every conversation row plus the transcript header carries a neutral symbol-and-text mode marker. Direct Chat mode is not supported. Legacy direct-chat rows in frontend storage are discarded rather than reinterpreted as Project requests. There is no in-conversation mode switch and no separate Act page; the legacy `/act` route redirects to `/chat`.

The desktop conversation workspace has a viewport-bounded fixed height. The conversation list and transcript scroll independently, and the composer remains anchored at the bottom of the transcript segment. The compact conversation list has no row separators or timestamps: each row uses a leading mode icon, title, and trailing mode text with blue Act and amber Project semantics, plus clear hover and selected surfaces. Deletion is available only for the active conversation as a red trash-icon button in the transcript header, with an accessible text label. New Project and New Act remain two distinct controls. On narrow screens the workspace becomes a stacked layout and releases the desktop height constraint.

Transcript messages have no horizontal separators. A fresh Project opens with “Hi, what can I build for you today?” and a fresh Act opens with “Hi, what can I do for you?” New Act creates its local conversation immediately and attaches the backend session without reusing message-send loading UI. Active Project and Act turns share one visible “Eidolon is thinking…” treatment; Act does not add a second polling placeholder. Each Act response includes a collapsed work-details row with persisted execution time and concise backend-recorded activity, including the tool name when Codex returns it. Raw model reasoning is not fetched, reconstructed, or displayed. User messages use a conventional right-aligned text bubble. Eidolon responses remain full-width transcript segments and use the visible role label **Eidolon**, never **assistant**. The composer uses a square return-key Send control with an accessible text label.

Build-time and runtime approvals are rendered inline in the chat transcript. Approval messages must remain in chat history when the user navigates away and returns. Project chat synchronizes its conversation-scoped generation request, linked agent run, proposed skill, and latest build/runtime approvals from the backend, so a response lost after a committed request or a decision made on the global Approval Requests page is recovered inline without duplicating messages.

Medium/high-risk caller-to-function relationships use normal Approval Requests entries with caller name, target function, description, derived risk, and explicit approval boundaries. Low-risk declared relationships are summarized during runtime review and do not create redundant approval rows.

Chat persistence is local frontend storage managed by `frontend/src/lib/chatStore.ts`. Every transcript is owned strictly by its conversation id: a temporarily unavailable selection renders a clean Project placeholder rather than falling back to another conversation, and the transcript subtree remounts when the selected id changes. Project conversations also store the pending generation request id while ProductManager is waiting for clarification so the user's next reply stays attached to the same request.
Users can delete any Project or Act conversation from the conversation list. Deletion removes the local transcript and asks the backend to remove any persisted message rows for the same frontend conversation id; Project-mode approval records remain available through the approval pages.

Approving a Project build-time request from either its inline chat card or the global Approval Requests page continues the linked agent run automatically. A separate Agent Run resume click is reserved for quota pauses, recoverable workflow failures, or explicit user-action blockers rather than ordinary permission approval.

`ChatPage` retains route-level API orchestration, imports active backend Act sessions, and polls only the selected Act conversation while its turn is queued or running. Deleting an Act conversation archives its backend session first; active work can be cancelled from the shared composer. `features/chat/useChatConversations.ts` owns local conversation selection, creation, deletion, drafts, fixed modes, Act-session bindings, and message transitions; it persists the synthesized first conversation before accepting edits. `features/chat/ChatWorkspace.tsx` owns the sidebar, fixed mode display, transcript, approval cards, and composer presentation.

## Skills Page

Skill, function, application, and schedule names are displayed from their canonical names by replacing underscores with spaces and capitalizing each word. Manifests do not carry a separate UI display name.

Lists skills in one list with their `function`, `service`, or `web_app` runtime. Each row opens Skill Detail, including web application rows; application launch remains on the Applications and Skill Detail pages. Services show schedule-managed availability rather than an enable/disable control. User-facing creation goes through the proposed-skill workflow, not bare database record creation.

## Functions Page

`/functions` displays the backend-owned unified catalog. It includes backend-core, installed user, and integration functions in every state, with id, description, category, risk, version, availability, and unavailability reasons. The page polls the catalog so enablement, deletion, dependency changes, and integration connection changes remain visible. Only available entries are injected into ProductManager prompts.

## Schedules Page

The top-level Schedules page is the only schedule management surface. It includes mutable generated-service schedules and read-only platform services. The table uses fixed column allocations so status, timestamp, and action changes do not shift the column boundaries; narrower viewports scroll the table instead of redistributing it. The Service column shows the service name and links generated services to Skill Detail; the Schedule column contains only the human-readable recurrence. Recurrence and next/last timestamps omit timezone names and abbreviations from the table; the Edit modal retains the timezone field that controls execution. Generated services support edit, pause/resume, and Run Now; Run Now also works while paused and does not resume the schedule. Actions are left-aligned, Pause and Resume share one stable width, and Edit opens the existing schedule form in a modal. There are no create, delete, or schedule-approval controls because every generated service owns exactly one required schedule. Last Run places a status-colored dot before the timestamp and exposes the status text from the dot's hover tooltip instead of repeating it inline. Schedule rows keep one consistent desktop height, including rows with action buttons. The daily Notion Done cleanup appears with next/last state and “Managed by Eidolon.”

## Skill Detail Page

Shows:

- skill metadata and status;
- files;
- validation;
- runtime permissions;
- runner/sandbox state;
- function run input, output, and run history;
- agent runs;
- versions and comparisons;
- update suggestion chat;
- schedules.

Installed function skills show a bounded JSON run-input panel and may be run manually only when backend checks pass. The returned result appears in a separate Output panel. Run History has no separate latest-run view: every stored run displays its output, status, error summary, token breakdown, and Codex call count inline. Service detail keeps the ordinary files, validation, permission, version, update, agent-run, and run-history panels, removes manual run and enable controls, and shows a concise required-schedule summary linking to Schedules. Web application details replace bounded-run input, output, history, and schedule controls with an Open Application action. Skill Detail presents one complete runtime approval containing the base manifest permissions, dependencies, and every current provider integration operation. One approve or deny action applies to all pending components of that displayed contract. Installation and execution require the complete current runtime contract to be approved.

`SkillDetailPage` retains route loading, polling, and mutation orchestration. Cohesive update-chat, version, comparison, validation, and run-detail presentation lives under `features/skill-detail/SkillDetailPanels.tsx`; service schedule editing belongs only to `SchedulesPage`. Feature tests cover conversation state transitions, chat workspace interactions, service/platform schedule visibility, version empty state, and run-input validation; `npm test` is the frontend regression command and `npm run build` remains the production type/build check.

## Applications Pages

`ApplicationsPage` lists only `runtime = web_app` skills. Function skills do not appear there and have no separate interface page in the current milestone. Each application card links separately to Open Application and Open Skill Details. Installed enabled web applications open through `/apps/:skillId`; the opened application chrome does not repeat the Skill Detail control.

`WebAppPage` asks the backend for a ready, version-pinned application session and keeps trusted identity, version, lifecycle status, containment disclosures, stop control, logs, and bounded audit status outside the frame. It embeds only an HTTP(S) hostname under the configured `*.web-app.localhost` gateway domain; ordinary external or credential-bearing URLs are rejected. The iframe uses `sandbox="allow-scripts allow-forms allow-same-origin allow-modals"`, an empty browser-feature allowlist, and `no-referrer`. The page coalesces Strict Mode mount replays into one session-open request. The backend supplies the complementary CSP and Permissions Policy. The page polls diagnostics while the application is healthy and displays readiness/failure details without trusting application-rendered status.

If the backend gateway domain is customized, `VITE_WEB_APP_GATEWAY_DOMAIN` must match it. The application runtime and containment contract is documented in [Sandboxed web applications](../runtime/web_applications.md).

## Agent Runs Pages

Agent Runs list and detail pages show run status, current task node or parallel active nodes, current step, step logs, DAG progress, node failures, and applicable retry/cancel controls. ProductManager, Builder, and Tester steps expose the exact complete prompt sent to Codex—including instructions and composed inputs—and the exact final response returned by Codex. Backend-only permission, dependency, validation, finalization, and bounded-stop steps are labeled `Backend` and show only a fixed summary, never agent input/output controls. Historical agent steps without a recoverable exact transcript are labeled unavailable instead of falling back to normalized workflow JSON. Task-DAG failures expose run-level and failed-step retry actions. Single-Codex errors are terminal and expose no retry action; only a pre-invocation quota pause may resume. Cancelling an active Codex-backed run terminates its owned Codex process and leaves the run cancelled; it cannot later become failed or create runtime approvals. The detail page has Build Details and Skill Run History tabs. Build Details renders the recorded task DAG using each stable node id, task prompt, status, dependencies, write paths, assigned function ids, per-node Codex tokens, build totals, usage pause reason, and resume control. Skill Run History lists runs for the linked skill with separate runtime Codex totals and per-invocation success/failure metadata, including retained CLI diagnostics for failed calls. Skill detail shows completed agent-run token totals.

## Codex Settings

Settings uses a shared section navigation so each concern has a focused URL and loads only the data it needs. `/settings` redirects to `/settings/usage`.

`/settings/appearance` lets the user choose Dark or Light. The choice is stored in local browser storage and applied immediately to the document. Dark is the default when no choice has been saved.

`/settings/usage` shows the resolved CLI and both the 5-hour and weekly Codex allowance windows. DAG builds pause before the next ready batch when either window has less than 5% remaining. Refresh reads current local App Server state; it does not infer quota from project-build token totals. Skill runtime calls remain excluded from build token accounting.

`/settings/project` lets the user keep automatic Project build workflow selection or force every new build through Simple (`single_codex`) or Task DAG (`task_dag`); forced selection overrides ProductManager output in the backend.

`/settings/models` labels the existing blueprint route **Project planning and clarification**. It loads the live App Server model catalog and lets the user choose model and reasoning effort independently for Act, ProductManager actions, the single-Codex Builder, Builder difficulty tiers plus repair/update, and Tester task/final/update actions. Model controls have an adjacent **Save model routing** action and show whether changes are unsaved or when the persisted routes were last saved. Unsupported model/effort combinations are rejected by the backend.

`/settings/integrations` includes the single GitHub connection. It shows connected/disconnected/unavailable state, validated account identity, last validation time, and sanitized errors, with add, replace, and remove actions. The token input is password-style, is cleared after submission, and is never returned or redisplayed.

The Integrations subpage also includes one shared Notion connection panel. The credential subsection comes first and adds or replaces the write-only private-connection token. A separate data-source subsection follows with Todo and Reports ID fields plus one Save and one Delete action that apply to both IDs. Source Save validates both exact schemas with the stored token before replacing either ID; source Delete clears both IDs but preserves the credential. The panel shows sanitized bot/workspace identity, both configured sources, status, and validation time. It explicitly states that Notion remains the Todo and Report UI and that Eidolon has no local copy, cache, or sync controls. The token is cleared after every submission and never redisplayed.

One Google connection panel owns a single write-only OAuth client ID and secret plus the Calendar and Gmail redirect URIs. Within it, Calendar and Gmail have separate sanitized status, verified account, Connect/Choose another account, and Disconnect controls. The shared client fields are disabled while either service is connected; replacing or removing it requires both grants to be disconnected. Starting either service leaves the page for that service's Google consent flow, and callbacks return only a bounded result marker. Calendar and Gmail never share a refresh token, scope grant, or account identity and may use different accounts. The panel documents Google's restricted `gmail.modify` scope. There is no calendar, event, synchronization, webhook, cache, secondary-calendar, or schedule UI.

One Telegram panel contains separate **Notification / Approval bot** and **Agent bot** subsections. Each has its own write-only token, sanitized bot/pairing state, one-time private-chat instructions, and replacement/disconnect controls. The notification bot subsection includes the complete-input cloud privacy disclosure. Pairing distinguishes `Awaiting private chat` from `Pairing expired`, and only reports connected after both private chat and user IDs are bound.

User-facing timestamps are rendered in the browser's system timezone with a short timezone label, except the Schedules table deliberately omits the label. Backend and SQLite timestamps without an explicit offset are treated as UTC before conversion; persisted timestamps and scheduler bookkeeping remain UTC internally. On an Eastern-time host this otherwise renders EDT or EST according to daylight-saving rules.

Approval Requests has separate **Permission approvals** and **Invocation approvals** tabs. The invocation view shows pending/history actions, the backend-snapshotted readable approval fields, caller-provided reason, Telegram delivery, decision/execution state, result/error, and local approve/deny controls. The frontend does not recognize individual function ids or independently format raw invocation JSON.

The Integrations subpage also includes local Eidolon-Atlas lifecycle and passphrase controls. It shows the selected directory, owned/external process state, initialized/locked state, saved-passphrase state, and bounded errors. Directory save restarts immediately. The write-only passphrase and **Unlock now** controls are enabled only for an Eidolon-owned process; an external process is explicitly directed to unlock through Atlas itself. The UI discloses that storing the passphrase shifts practical at-rest protection to the Windows account. Submitted passphrases are cleared and never redisplayed.

The Integrations subpage includes a compact **Codex tools** panel. It shows enabled, registered, configuration-match, available-tool, excluded-ID, and bounded-error status. Install, Repair, and Remove call the owned `/settings/codex-mcp` lifecycle. The panel explains that new or restarted Codex Desktop, CLI, and IDE sessions discover the current catalog and that open sessions are not hot-refreshed.

`/settings/permissions` shows the read-only current permission policy loaded from `/settings/permission-policy`: default-allowed capabilities, the exact approval-required template, blocked capabilities, the web-application policy, and the checked-in source path. The frontend does not embed a second policy copy.

Runtime approval uses the existing `PermissionRequestModal`. Its single review shows each provider, operation id, read/write behavior, normalized scope, current authorization state, and per-operation connection availability alongside the base manifest permissions. Notion has no caller-selected scope; Todo list and Report list/get are low-risk reads while Todo create/update/delete and Report create/delete are medium-risk writes. Google Calendar likewise has no caller-selected scope: list/get are low-risk reads and create/update/delete are medium-risk writes, all fixed to `primary`. Connection state alone never marks a skill approved.

Atlas reviews use the same modal with empty resource scope. Reads are low risk; `atlas.knowledge.node.know` is medium risk and describes its internet-enabled Codex call and bounded Knowledge write.

## Targeted Polling

The MVP uses targeted polling, not push events. Pages with changing backend state poll only relevant resources and should stop or slow down once records reach terminal states.
