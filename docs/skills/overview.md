# Skill Overview

A skill is a reusable capability package managed by the local assistant.

Every skill contains executable Python code and tests. A skill may optionally include `SKILL.md` reusable instructions or operating guidance.

## Runtime Protocols

- `function` is the bounded one-shot Python JSON stdin/stdout protocol. Function skills may run directly or through the backend Function registry, but have no dedicated interface page and cannot be scheduled.
- `service` uses the same bounded JSON stdin/stdout execution protocol as a scheduler-only endpoint. Every service has exactly one required schedule, cannot be invoked manually or by another agent/skill/MCP caller, and has no dedicated interface page. While running, it may consume only its declared functions, integrations, and Codex capability.
- `web_app` is a persistent importable ASGI protocol. It owns self-rendered HTML/CSS/JavaScript and interaction inside its package and is opened through the Applications UI on a controlled untrusted origin.

Runtime is both the execution contract and the sole interface discriminator: `web_app` packages appear in Applications, while `function` and `service` packages do not receive a user-facing application interface. Services are managed through their ordinary Skill Detail and the shared Schedules page; there is no Services page. Backend-core, installed user, and integration functions share one backend-owned catalog. Invocation still requires current runtime eligibility plus an explicit caller manifest relationship or integration authorization. See [Function registry and invocation](../runtime/functions.md).

## Skill Folders

Proposed skills:

```text
skills/proposed/<skill_name>/
```

Installed skills:

```text
skills/installed/<skill_name>/versions/vN/
```

The active installed skill points to one active version folder.

## Required Package Files

Every skill package needs a valid `manifest.json`.

`README.md` is optional.

Every skill needs an executable Python entrypoint referenced by `entrypoint` and tests written or maintained by TesterAgent. Function and service skills usually declare `skill.py`; web applications declare an importable ASGI target such as `app:app` and may include package-owned web assets.

`SKILL.md` is optional. When reusable instructions are useful, `instructions_path` should reference that file, usually `SKILL.md`.

ProductManager controls the product structure beyond these platform minimums. Workflow artifacts such as `intent_prompt.json`, `decision.json`, `blueprint.json`, `permissions.json`, `task_dag.json`, `tasks/*.json`, and task `interface_artifact.json` files are platform artifacts, not skill package files. Builder temporarily writes `interface_artifact.json` inside the controlled skill folder; the backend validates and moves it into the run-artifact folder before the package can proceed.

Generated web applications never modify the Eidolon frontend. See [Sandboxed web applications](../runtime/web_applications.md) for the ownership, gateway, state, and lifecycle contract.
