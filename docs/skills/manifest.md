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
    "shell": false
  },
  "schedule": null,
  "created_by": "codex",
  "enabled": false
}
```

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

Hybrid skills:

- require both `instructions_path` and `entrypoint`;
- require the declared instruction and entrypoint files to exist;
- require tests;
- may request supported runtime permissions.

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
- summarize public text.

Blocked in MVP:

- shell access;
- secrets;
- arbitrary filesystem reads;
- broad writes;
- wildcard network;
- browser automation;
- high-risk third-party actions.
