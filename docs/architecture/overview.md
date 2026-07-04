# Architecture Overview

The project is a local-first control plane for reusable application skills. The assistant can chat, store explicit memory, propose skills, build them with Codex-backed agents, validate them, request approvals, install them, run them in a sandbox, schedule them, and update them through versioned drafts.

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

- Chat with chat/project modes;
- Memory CRUD;
- Skills list and skill detail;
- Tools list/detail;
- Schedules;
- Approval Requests;
- Agent Runs and step logs.

Targeted polling refreshes changing workflow state. The MVP does not use SSE or WebSockets.

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
  docker_runner_build.json
```

Generated skills must not modify backend/frontend app source.

## Current Constraints

The MVP is local and single-user. It intentionally avoids multi-user auth, cloud orchestration, autonomous background agents, unrestricted shell access, browser automation, secrets access, high-risk third-party actions, and silent package installation.
