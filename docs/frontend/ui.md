# Frontend UI

The frontend is a React/Vite TypeScript app under `frontend/src`.

## Navigation

Routes are defined in `frontend/src/App.tsx`:

- `/chat`
- `/memory`
- `/skills`
- `/tools`
- `/tools/:skillId`
- `/skills/:skillId`
- `/schedules`
- `/agent-runs`
- `/agent-runs/:agentRunId`
- `/approval-requests`
- `/settings/usage`

## API Client

`frontend/src/api/client.ts` defines API types and request helpers. Frontend types mirror backend schemas for skills, runs, versions, approvals, schedules, tools, generation requests, and agent runs.

## Chat Page

Chat supports explicit modes:

- `chat`: direct conversation only.
- `project`: starts ProductManager intent refinement and plausibility review for a reusable skill request. ProductManager may ask a clarification question before any blueprint, permission, or task DAG artifacts are created; the next reply in the same chat continues the same generation request.

Build-time and runtime approvals are rendered inline in the chat transcript. Approval messages must remain in chat history when the user navigates away and returns.

Chat persistence is local frontend storage managed by `frontend/src/lib/chatStore.ts`. Project conversations also store the pending generation request id while ProductManager is waiting for clarification so the user's next reply stays attached to the same request.
Users can delete any chat conversation from the chat list. Deletion removes the local transcript and asks the backend to remove any persisted message rows for the same frontend conversation id; Project-mode approval records remain available through the approval pages.

## Skills Page

Lists skills in one list. User-facing creation goes through the proposed-skill workflow, not bare database record creation.

## Skill Detail Page

Shows:

- skill metadata and status;
- files;
- validation;
- runtime permissions;
- runner/sandbox state;
- run history;
- agent runs;
- versions and comparisons;
- update suggestion chat;
- schedules.

Installed executable skills may be run manually only when backend checks pass.
Run history shows each run's separate runtime Codex token total, and the latest-run detail includes its token breakdown and call count.

## Tools Pages

`ToolsPage` lists installed enabled automation skills with `interface_type = "tool"`. `ToolDetailPage` renders a form from declarative `tool_ui_schema` when present, otherwise it falls back to JSON input. Tool runs use the same backend runner and permission checks as skill runs.

## Agent Runs Pages

Agent Runs list and detail pages show run status, current task node or parallel active nodes, current step, step logs, structured inputs/outputs, DAG progress, node failures, and retry/cancel controls. The detail page has Build Details and Skill Run History tabs. Build Details renders the recorded task DAG with task node status, dependencies, expected output paths, file write claims, backend API ids, per-node Codex tokens, build totals, usage pause reason, and resume control. Skill Run History lists runs for the linked skill with separate runtime Codex totals and per-invocation metadata. Skill detail shows completed agent-run token totals.

## Codex Settings

`/settings/usage` shows the resolved CLI and both the 5-hour and weekly Codex allowance windows. It lets the user keep automatic Project build workflow selection or force every new build through Simple (`single_codex`) or Task DAG (`task_dag`); forced selection overrides ProductManager output in the backend. The page also loads the live App Server model catalog and lets the user choose model and reasoning effort independently for Chat, ProductManager actions, Builder difficulty tiers plus repair/update, and Tester task/final/update actions. Unsupported model/effort combinations are rejected by the backend. Refresh reads current local App Server state; it does not infer quota from project-build token totals.

## Targeted Polling

The MVP uses targeted polling, not push events. Pages with changing backend state poll only relevant resources and should stop or slow down once records reach terminal states.
