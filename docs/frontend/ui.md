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
- `/settings/usage`

## API Client

`frontend/src/api/client.ts` defines API types and request helpers. Frontend types mirror backend schemas for skills, runs, versions, approvals, schedules, generation requests, and agent runs.

## Chat Page

Chat supports explicit modes:

- `chat`: direct conversation only.
- `project`: writes the initial message unchanged through the one-time intent placeholder, then starts a resumable `pm_plan_build` session for a reusable skill request. The placeholder does not call Codex. The session may ask a clarification question or reject the request before any blueprint, permission, or task DAG artifacts are created; the next reply in the same chat resumes the same generation request and Codex thread.

Build-time and runtime approvals are rendered inline in the chat transcript. Approval messages must remain in chat history when the user navigates away and returns. Project chat synchronizes its conversation-scoped generation request, linked agent run, proposed skill, and latest build/runtime approvals from the backend, so a response lost after a committed request or a decision made on the global Approval Requests page is recovered inline without duplicating messages.

Medium/high-risk caller-to-function relationships use normal Approval Requests entries with caller name, target function, description, derived risk, and explicit approval boundaries. Low-risk declared relationships are summarized during runtime review and do not create redundant approval rows.

Chat persistence is local frontend storage managed by `frontend/src/lib/chatStore.ts`. Project conversations also store the pending generation request id while ProductManager is waiting for clarification so the user's next reply stays attached to the same request.
Users can delete any chat conversation from the chat list. Deletion removes the local transcript and asks the backend to remove any persisted message rows for the same frontend conversation id; Project-mode approval records remain available through the approval pages.

Approving a Project build-time request from either its inline chat card or the global Approval Requests page continues the linked agent run automatically. A separate Agent Run resume click is reserved for quota pauses, recoverable workflow failures, or explicit user-action blockers rather than ordinary permission approval.

`ChatPage` retains route-level API orchestration. `features/chat/useChatConversations.ts` owns local conversation selection, creation, deletion, drafts, modes, and message transitions; it persists the synthesized first conversation before accepting edits. `features/chat/ChatWorkspace.tsx` owns the sidebar, mode selector, transcript, approval cards, and composer presentation.

## Skills Page

Lists skills in one list with their `function` or `web_app` runtime. Installed enabled web applications expose an Open Application action. User-facing creation goes through the proposed-skill workflow, not bare database record creation.

## Functions Page

`/functions` displays the backend-owned unified catalog. It includes backend-core, installed user, and integration functions in every state, with id, description, category, risk, version, availability, and unavailability reasons. The page polls the catalog so enablement, deletion, dependency changes, and integration connection changes remain visible. Only available entries are injected into ProductManager prompts.

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

Installed function skills show a bounded JSON run-input panel and may be run manually only when backend checks pass. The returned result appears in a separate Output panel. Run History has no separate latest-run view: every stored run displays its output, status, error summary, token breakdown, and Codex call count inline. Web application details replace bounded-run input, output, history, and schedule controls with an Open Application action while retaining files, validation, permission, version, update, and agent-run controls.

`SkillDetailPage` retains route loading, polling, and mutation orchestration. Cohesive update-chat, version, comparison, schedule, validation, and run-detail presentation lives under `features/skill-detail/SkillDetailPanels.tsx`. Feature tests cover conversation state transitions, chat workspace interactions, schedule delegation, version empty state, and run-input validation; `npm test` is the frontend regression command and `npm run build` remains the production type/build check.

## Applications Pages

`ApplicationsPage` lists only `runtime = web_app` skills. Function skills do not appear there and have no separate interface page in the current milestone. Installed enabled web applications open through `/apps/:skillId`.

`WebAppPage` asks the backend for a ready, version-pinned application session and keeps trusted identity, version, lifecycle status, containment disclosures, stop control, logs, and bounded audit status outside the frame. It embeds only an HTTP(S) hostname under the configured `*.web-app.localhost` gateway domain; ordinary external or credential-bearing URLs are rejected. The iframe uses `sandbox="allow-scripts allow-forms allow-same-origin allow-modals"`, an empty browser-feature allowlist, and `no-referrer`. The page coalesces Strict Mode mount replays into one session-open request. The backend supplies the complementary CSP and Permissions Policy. The page polls diagnostics while the application is healthy and displays readiness/failure details without trusting application-rendered status.

If the backend gateway domain is customized, `VITE_WEB_APP_GATEWAY_DOMAIN` must match it. The application runtime and containment contract is documented in [Sandboxed web applications](../runtime/web_applications.md).

## Agent Runs Pages

Agent Runs list and detail pages show run status, current task node or parallel active nodes, current step, step logs, DAG progress, node failures, and applicable retry/cancel controls. ProductManager, Builder, and Tester steps expose the exact complete prompt sent to Codex—including instructions and composed inputs—and the exact final response returned by Codex. Backend-only permission, dependency, validation, finalization, and bounded-stop steps are labeled `Backend` and show only a fixed summary, never agent input/output controls. Historical agent steps without a recoverable exact transcript are labeled unavailable instead of falling back to normalized workflow JSON. Task-DAG failures expose run-level and failed-step retry actions. Single-Codex errors are terminal and expose no retry action; only a pre-invocation quota pause may resume. The detail page has Build Details and Skill Run History tabs. Build Details renders the recorded task DAG using each stable node id, task prompt, status, dependencies, write paths, assigned function ids, per-node Codex tokens, build totals, usage pause reason, and resume control. Skill Run History lists runs for the linked skill with separate runtime Codex totals and per-invocation success/failure metadata, including retained CLI diagnostics for failed calls. Skill detail shows completed agent-run token totals.

## Codex Settings

`/settings/usage` shows the resolved CLI and both the 5-hour and weekly Codex allowance windows. It also shows the read-only current permission policy loaded from `/settings/permission-policy`: default-allowed capabilities, the exact approval-required template, blocked capabilities, the web-application policy, and the checked-in source path. The frontend does not embed a second policy copy.

The page labels the existing blueprint route **Project planning and clarification**. It lets the user keep automatic Project build workflow selection or force every new build through Simple (`single_codex`) or Task DAG (`task_dag`); forced selection overrides ProductManager output in the backend. It also loads the live App Server model catalog and lets the user choose model and reasoning effort independently for Chat, ProductManager actions, the single-Codex Builder, Builder difficulty tiers plus repair/update, and Tester task/final/update actions. Model controls have an adjacent **Save model routing** action and show whether changes are unsaved or when the persisted routes were last saved. Unsupported model/effort combinations are rejected by the backend. Refresh reads current local App Server state; it does not infer quota from project-build token totals.

The same trusted Settings page includes the single GitHub connection. It shows connected/disconnected/unavailable state, validated account identity, last validation time, and sanitized errors, with add, replace, and remove actions. The token input is password-style, is cleared after submission, and is never returned or redisplayed.

Settings also includes local Eidolon-Atlas lifecycle and passphrase controls. It shows the selected directory, owned/external process state, initialized/locked state, saved-passphrase state, and bounded errors. Directory save restarts immediately. The write-only passphrase and **Unlock now** controls are enabled only for an Eidolon-owned process; an external process is explicitly directed to unlock through Atlas itself. The UI discloses that storing the passphrase shifts practical at-rest protection to the Windows account. Submitted passphrases are cleared and never redisplayed.

Runtime approval uses the existing `PermissionRequestModal`. GitHub `integration_access` reviews show provider, operation ids, read-only status, normalized repositories, and current connection availability. Connection state alone never marks a skill approved.

Atlas reviews use the same modal with empty resource scope. Reads are low risk; `atlas.knowledge.node.know` is medium risk and describes its internet-enabled Codex call and bounded Knowledge write.

## Targeted Polling

The MVP uses targeted polling, not push events. Pages with changing backend state poll only relevant resources and should stop or slow down once records reach terminal states.
