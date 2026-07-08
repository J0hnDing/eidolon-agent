# Skill Manifest

`manifest.json` is the source of truth for a skill package.

For new Project-mode builds, the backend derives an initial skeleton `manifest.json` from the approved `blueprint.json` and `permissions.json` before Builder writes implementation files. Builder may complete task-owned details, and the backend later fills missing manifest fields deterministically when possible before final validation and runtime permission review.

## Core Fields

```json
{
  "name": "example_skill",
  "description": "What this skill does.",
  "skill_type": "automation",
  "interface_type": "chat",
  "entrypoint": "skill.py",
  "instructions_path": null,
  "input_schema": null,
  "output_schema": null,
  "tool_ui_schema": null,
  "dependencies": [],
  "risk_level": "low",
  "permissions": {
    "network": [],
    "filesystem_read": [],
    "filesystem_write": ["./cache"],
    "secrets": [],
    "shell": false,
    "codex": {
      "call_response": true,
      "internet_access": false
    }
  },
  "schedule": null,
  "created_by": "codex",
  "enabled": false
}
```

`schedule` is ProductManager-owned manifest intent for recurring execution. Use `null` when no recurring run was requested. Supported executable schedule forms are:

- `{"type": "daily", "time": "HH:MM", "timezone": "America/Toronto", "input": {}}`;
- `{"type": "weekly", "day": "monday", "time": "HH:MM", "timezone": "America/Toronto", "input": {}}`;
- `{"type": "interval", "every": 1, "unit": "hours", "timezone": "America/Toronto", "input": {}}`.

On install, the backend registers a manifest-declared schedule as a pending schedule record and creates a schedule approval request. It does not activate the schedule automatically.

## Skill Type Rules

Instruction skills:

- require `instructions_path`;
- do not require `entrypoint`;
- must request no executable permissions;
- are not runnable.

Automation skills:

- require `entrypoint`;
- require the declared entrypoint file to exist;
- require tests;
- may request supported runtime permissions.

Automation skills may optionally include reusable instructions:

- set `instructions_path` only when an instructions file is present;
- usually use `SKILL.md` for that optional instructions file;
- still require `entrypoint`, tests, and supported runtime permissions review.

## Interface Type Rules

`interface_type` must be one of:

```text
chat, tool, hidden
```

Tool skills should provide declarative `tool_ui_schema`. Generated skills must not inject React, HTML, JavaScript, or app frontend code.

## Dependency Rules

`dependencies` is optional and contains Python package names or simple version specifiers. URLs, Git references, local paths, editable installs, shell flags, and generated-skill Dockerfiles are blocked.

Build-time dependency installation is approval-gated. Runtime package installation is not supported.

## Permission Risk

Low-risk examples:

- no network;
- explicit public network domains;
- read/write to the skill's own `./cache`;
- backend-mediated Codex call/response without Codex internet access;
- summarize public text.

Codex permissions:

- `call_response` defaults to `true` for all skills. It allows a skill to ask the backend for a Codex text response through `POST /skills/{skill_id}/codex`.
- `internet_access` may be `true` only when the skill also has approved runtime network domains. Runtime network access and Codex internet access are treated as equivalent for approval.
- Any other Codex permission field is blocked in this milestone.

Blocked in MVP:

- shell access;
- secrets;
- arbitrary filesystem reads;
- broad writes;
- wildcard network;
- browser automation;
- high-risk third-party actions.
