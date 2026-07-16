# Skill Manifest

`manifest.json` is the explicit declaration of skill package and runtime intent. It is not the source of lifecycle state.

For new Project-mode builds, the backend derives an initial skeleton from the approved `blueprint.json` and `permissions.json` before Builder writes implementation files. Builder may complete package-owned details, and the backend fills only missing approved fields before parsing the actual manifest, checking runtime agreement with the blueprint, scanning for capability mismatches, running tests, and starting runtime permission review. Scanning never silently infers or expands declarations.

## Core Fields

```json
{
  "manifest_version": 1,
  "name": "example_skill",
  "description": "What this skill does.",
  "runtime": "function",
  "entrypoint": "skill.py",
  "instructions_path": null,
  "input_schema": null,
  "output_schema": null,
  "dependencies": [],
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
  "schedule": null
}
```

Installation state, enabled state, active version, provenance, and risk level are backend-owned. They are not canonical manifest fields. The validator reads older packages that still contain `risk_level`, `created_by`, or `enabled` for compatibility, but canonical serialization and new generation omit them. Risk is derived deterministically from permissions and dependencies when the backend updates the skill record.

`schedule` is ProductManager-owned manifest intent for recurring bounded function execution. Use `null` when no recurring run was requested. `web_app` manifests must use `null`; persistent services are not scheduled `SkillRun` jobs. Supported function schedule forms are:

- `{"type": "daily", "time": "HH:MM", "timezone": "America/Toronto", "input": {}}`;
- `{"type": "weekly", "day": "monday", "time": "HH:MM", "timezone": "America/Toronto", "input": {}}`;
- `{"type": "interval", "every": 1, "unit": "hours", "timezone": "America/Toronto", "input": {}}`.

On install, the backend registers a manifest-declared schedule as a pending schedule record and creates a schedule approval request. It does not activate the schedule automatically.

## Runtime Rules

- `runtime` is `function` or `web_app`;
- both protocols require an `entrypoint` and tests;
- `function` uses a relative Python file such as `skill.py` and bounded JSON stdin/stdout execution;
- `web_app` uses importable `module:attribute` ASGI syntax such as `app:app` and cannot declare a bounded schedule;
- the declared entrypoint file or module must exist inside the package;
- may request supported runtime permissions.

Skills may optionally include reusable instructions:

- set `instructions_path` only when an instructions file is present;
- usually use `SKILL.md` for that optional instructions file;
- optional instructions do not change the executable entrypoint, test, or runtime permission requirements.

## Interface Exposure

`runtime` is the sole interface discriminator. A `web_app` owns its HTML, CSS, and JavaScript inside the skill folder and is opened from Applications. Generated skills must never modify or inject source into the Personal Agent React frontend. A `function` has no dedicated interface surface in the current milestone.

## Dependency Rules

`dependencies` is optional and contains Python package names or simple version specifiers. URLs, Git references, local paths, editable installs, shell flags, and generated-skill Dockerfiles are blocked.

Build-time dependency installation is approval-gated. Runtime package installation is not supported.

## Permission Risk

Low-risk examples:

- no network;
- read/write to the skill's own `./cache`;
- backend-mediated Codex call/response without Codex internet access;
- summarize public text.

Network domains, third-party dependencies, Codex internet access, and non-cache filesystem declarations derive at least medium risk. Unsupported shell/secrets declarations derive high risk and are blocked by current policy.

Codex permissions:

- `call_response` defaults to `true`. Function skills use `POST /skills/{skill_id}/codex` through the existing bounded runtime integration. Web applications must instead use the trusted server-side `web_runtime_capabilities.call_codex` helper, which authenticates `POST /web-apps/capabilities/codex` with an instance-scoped capability rather than a caller-supplied skill id.
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
