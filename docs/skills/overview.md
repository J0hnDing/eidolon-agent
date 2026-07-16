# Skill Overview

A skill is a reusable capability package managed by the local assistant.

Every skill contains executable Python code and tests. A skill may optionally include `SKILL.md` reusable instructions or operating guidance.

## Interface Types

`interface_type` describes how an installed skill is exposed:

- `chat`: primarily used through chat.
- `tool`: appears as a manual form/tool in the Tools UI when installed and enabled.
- `hidden`: not shown as a normal user-facing entry point.

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

Every skill needs an executable Python entrypoint referenced by `entrypoint`, usually `skill.py`, and tests written or maintained by TesterAgent.

`SKILL.md` is optional. When reusable instructions are useful, `instructions_path` should reference that file, usually `SKILL.md`.

ProductManager controls the product structure beyond these platform minimums. Workflow artifacts such as `intent_prompt.json`, `decision.json`, `blueprint.json`, `permissions.json`, `task_dag.json`, `tasks/*.json`, and task `interface_artifact.json` files are platform artifacts, not skill package files. Builder temporarily writes `interface_artifact.json` inside the controlled skill folder; the backend validates and moves it into the run-artifact folder before the package can proceed.
