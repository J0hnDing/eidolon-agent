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
  "input_schema": {
    "type": "object",
    "properties": {},
    "additionalProperties": true
  },
  "output_schema": {
    "type": "object",
    "properties": {},
    "additionalProperties": true
  },
  "requires_invocation_approval": false,
  "function_requirements": ["installed_function_name"],
  "integration_requirements": [],
  "dependencies": [],
  "permissions": {
    "codex": {
      "call_response": true
    }
  },
  "schedule": null
}
```

Installation state, enabled state, active version, provenance, and risk level are backend-owned. They are not canonical manifest fields. The validator reads older packages that still contain `risk_level`, `created_by`, or `enabled` for compatibility, but canonical serialization and new generation omit them. Risk is derived deterministically from permissions, dependencies, integrations, and the transitive function graph when the backend validates or updates the skill record.

`name` is the only skill-name field. Manifests do not accept `display_name`; user interfaces derive readable labels from `name` by replacing underscores with spaces and capitalizing each word.

`permissions` contains approval-gated requests only. New manifests omit backend defaults such as the Python standard library and the skill's own `./cache` access, and they never repeat blocked capabilities such as shell or secrets. An empty object means the skill requests no approval-gated runtime capability. Older safe manifests that explicitly list default values remain readable. Explicit `codex.call_response: false` is also valid and is equivalent to omitting that request.

The ProductManager blueprint declares object-shaped `input_schema` and `output_schema` JSON Schemas for new function and service skills. Builder implements that contract but does not redefine it. Service schemas are required. The backend validates the schemas themselves and validates every registry function input/output and every scheduled service input/output against them. Older installed functions with missing schemas remain directly runnable, but appear unavailable for cross-skill registry invocation until updated with explicit contracts.

`requires_invocation_approval` defaults to `false` and is valid only for `function`. ProductManager may set it to `true` only when the user explicitly requires approval before every run or the function is high risk. During validation the backend forces the function's effective risk to high whenever the flag is true. The backend derives the public `reason_to_call` input and pending-receipt output without changing the authored schemas. The authored input must not define the reserved `reason_to_call` field. This flag is a per-call execution boundary, not a permission request. See [Invocation approvals](../security/invocation_approvals.md).

`function_requirements` declares caller relationships as exact installed user-function names. ProductManager selects unified catalog ids in the blueprint; the backend derives this manifest list for selected user functions. There is no per-function reason field. Requirements are not Python dependencies. The backend derives each skill's effective risk as at least the highest risk of every transitive child and derives its reviewed permission contract as the union of all transitive child permissions. Integrations contribute risk but an empty permission set. A skill cannot require itself, duplicate target names are invalid, and graph cycles or missing/deleted children fail validation.

`integration_requirements` declares trusted provider authorization separately from ordinary network permission. The backend groups blueprint-selected operations into one entry per provider. GitHub entries contain exact normalized repository scope when required; Atlas, Notion, Gmail, and Telegram entries use an empty caller-selected resource scope, with provider containment fixed by trusted connection settings. Wildcards, credentials, duplicate providers, and operations assigned to the wrong provider are invalid. See [GitHub integration](../integrations/github.md), [Atlas integration](../integrations/atlas.md), [Notion integration](../integrations/notion.md), [Gmail integration](../integrations/gmail.md), and [Telegram integration](../integrations/telegram.md).

`schedule` seeds the initial runtime schedule for a `service`. A service manifest must declare exactly one schedule. Function and web-app manifests must use `null`. Supported service schedule forms are:

- `{"type": "daily", "time": "HH:MM", "timezone": "America/Toronto", "input": {}}`;
- `{"type": "weekly", "day": "monday", "time": "HH:MM", "timezone": "America/Toronto", "input": {}}`;
- `{"type": "interval", "every": 1, "unit": "hours", "timezone": "America/Toronto", "input": {}}`.

On install, the backend creates exactly one paused `SkillSchedule` row from this manifest intent. The schedule is backend runtime state after installation: edits made on the Schedules page survive version activation, and a version update must remain compatible with the stored schedule input rather than overwriting it.

## Runtime Rules

- `runtime` is `function`, `service`, or `web_app`;
- all runtime protocols require an `entrypoint` and tests;
- `function` uses a relative Python file such as `skill.py` and bounded JSON stdin/stdout execution;
- `service` uses the same bounded JSON protocol, requires object-shaped input/output schemas and one schedule, and can run only from that schedule;
- registry-callable functions require object-shaped input/output JSON Schema contracts;
- all runtimes may declare `function_requirements` for backend-controlled server-side calls, but a service itself is never a registry target;
- `function` and `web_app` manifests cannot declare a schedule;
- `web_app` uses importable `module:attribute` ASGI syntax such as `app:app`;
- the declared entrypoint file or module must exist inside the package;
- may request supported runtime permissions.

Skills may optionally include reusable instructions:

- set `instructions_path` only when an instructions file is present;
- usually use `SKILL.md` for that optional instructions file;
- optional instructions do not change the executable entrypoint, test, or runtime permission requirements.

## Interface Exposure

`runtime` is the sole interface discriminator. A `web_app` owns its HTML, CSS, and JavaScript inside the skill folder and is opened from Applications. Generated skills must never modify or inject source into the Eidolon React frontend. Functions and services have no dedicated interface surface; services are managed through Skill Detail and Schedules only.

## Dependency Rules

`dependencies` is optional and contains Python package names or simple version specifiers. URLs, Git references, local paths, editable installs, shell flags, and generated-skill Dockerfiles are blocked.

Build-time dependency provisioning is approval-gated and backend-owned. Immediately after approval, declared runtime packages are installed into the proposed skill's `.deps` folder before Codex starts; missing build-only tools use `.build-deps`. Final validation requires the actual manifest dependency list to match the provisioned runtime contract exactly. Agents and generated code cannot install packages, and runtime package installation is not supported.

## Permission Risk

Low-risk examples:

- no network;
- read/write to the skill's own `./cache`;
- approved backend-mediated Codex call/response without Codex internet access;
- summarize public text.

Network domains, third-party dependencies, Codex internet access, and non-cache filesystem declarations derive at least medium risk. Unsupported shell/secrets declarations derive high risk and are blocked by current policy. `requires_invocation_approval=true` also forces high risk. A parent skill's effective risk can only increase when child-function or integration risk is folded into its graph contract.

Codex permissions:

- `call_response` defaults to `false` and requires explicit approval when `true`. Function and service skills use the trusted `function_runtime_capabilities.call_codex` helper, which authenticates `POST /functions/capabilities/codex` with the ephemeral bounded-run capability. For a service, that capability is valid only during its scheduled run. Web applications use the parallel trusted server-side `web_runtime_capabilities.call_codex` helper, which authenticates `POST /web-apps/capabilities/codex` with an instance-scoped capability. Neither sandbox transport trusts a caller-supplied skill id.
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
