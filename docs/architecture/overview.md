# Architecture Overview

Eidolon is a local-first control plane for reusable application skills. The assistant can plan Project requests, perform persistent Act work, store explicit memory, propose skills, build them with Codex-backed agents, validate them, request approvals, install them, run bounded function skills, host persistent sandboxed web applications, execute scheduler-only services, and update skills through versioned drafts.

## Main Parts

```text
Frontend React UI
  -> FastAPI backend routes
    -> service layer
      -> SQLite models
      -> filesystem skill folders
      -> runtime artifacts
      -> Codex CLI adapters
      -> Docker/local skill runners
      -> trusted web-app gateway and scoped capabilities
```

## Backend

The backend is organized around explicit service boundaries:

- routers expose FastAPI endpoints;
- schemas define API and manifest contracts;
- models define SQLite tables;
- services implement behavior such as permission review, proposed skill management, agent workflows, runners, scheduling, and versioning.

The backend is the safety boundary. The frontend may disable buttons or show warnings, but backend services must enforce install/run/update/delete constraints.

## Frontend

The frontend is a local control UI. It exposes:

- Project conversations and persistent Act sessions;
- persistent Act sessions backed by local Codex App Server threads and a controlled shared workspace;
- Memory CRUD;
- Skills list and skill detail;
- Schedules;
- Approval Requests;
- Agent Runs and step logs.
- Applications list and trusted application chrome around isolated skill-owned UI.

Targeted polling refreshes changing workflow and application lifecycle state. The MVP does not use SSE or WebSockets.

## Skill Filesystem

Skills live in controlled folders:

```text
skills/
  proposed/<skill_name>/
  installed/<skill_name>/versions/vN/
```

Runtime artifacts live under:

```text
runtime/
  agent_runs/
  skill_cache/
  web_apps/
  docker_runner_build.json
```

Generated skills must not modify backend/frontend app source.

## Skill Runtime Protocols

The manifest `runtime` discriminator selects an execution protocol:

- `function`: the bounded Python JSON stdin/stdout runner and backend-owned dynamic Function registry;
- `service`: the same bounded runner behind one required backend schedule, with no manual, registry, agent, skill, MCP, or interface exposure;
- `web_app`: a version-pinned importable ASGI service with separate application-instance, browser-session, gateway, and bounded audit records.

The backend is the control plane for all three protocols. One persistent catalog describes backend-core, installed user, and integration functions and their current availability; service skills are deliberately excluded from it. Invocation authority is still derived from active manifests, current versions, runtime eligibility, integration state, and caller-target approval. Web-app content remains on a distinct untrusted origin inside a sandboxed iframe; the React UI retains trusted navigation, lifecycle, version, permission, schedule, and function-catalog controls. See [Function registry and invocation](../runtime/functions.md), [Scheduling](../runtime/scheduling.md), and [Sandboxed web applications](../runtime/web_applications.md).

An explicitly installed local STDIO MCP server snapshots the available eligible catalog for Codex Desktop, CLI, and IDE sessions. It is separate from Eidolon's Project and Act surfaces and does not start the FastAPI lifecycle. Calls still pass through the trusted function registry or integration services and recheck a shared enabled state plus current availability and contract identity. See [Codex MCP tools](../integrations/codex_mcp.md).

## Trusted Integrations

GitHub, local Eidolon-Atlas, Notion Todos/Reports, and Google Calendar are trusted providers. GitHub and Notion use write-only tokens; Google Calendar uses a backend-owned OAuth authorization-code flow; Atlas uses its native API whenever it is running and unlocked, with an optional stored passphrase only for Eidolon-owned process unlock. Skills declare exact registry operations and receive provider-specific approval. GitHub retains exact repository scope, Atlas has no caller-selected resource scope, Notion fixes separate configured Todo and Reports data sources, and Google Calendar fixes every event operation to the authenticated account's primary calendar. Function and web-application containers call one scoped relay helper, and the backend performs provider traffic and returns normalized data. Eidolon never mirrors Notion content or Google Calendar events locally. See [GitHub integration capability](../integrations/github.md), [Eidolon-Atlas integration](../integrations/atlas.md), [Notion integration](../integrations/notion.md), and [Google Calendar integration](../integrations/google_calendar.md).

## Current Constraints

The MVP is local and single-user. It intentionally avoids multi-user auth, cloud orchestration, autonomous background agents, unrestricted shell access, browser automation, secrets access, high-risk third-party actions, and silent package installation. Explicit memory facts are implemented, but automatic context selection, memory-aware agent workflows, outcome learning, and long-term adaptation are not.
