# Skill Overview

A skill is a reusable capability package managed by the local assistant.

## Skill Types

- `instruction`: reusable instructions only, no executable code.
- `automation`: executable Python automation.
- `hybrid`: reusable instructions plus executable Python automation.

## Interface Types

`interface_type` describes how an installed skill is exposed:

- `chat`: primarily used through chat.
- `tool`: appears as a manual form/tool in the Tools UI when installed and enabled.
- `hidden`: not shown as a normal user-facing entry point.

`tool` is not a `skill_type`.

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

Every skill package needs:

- `manifest.json`
- `README.md`

Instruction and hybrid skills need an instructions file referenced by `instructions_path`, usually `SKILL.md`.

Automation and hybrid skills need an executable Python entrypoint referenced by `entrypoint`, usually `skill.py`, and tests written/maintained by TesterAgent.

ProductManager controls the product structure beyond these platform minimums. Workflow artifacts such as `blueprint.json`, `permissions.json`, and `milestones/*.json` are platform artifacts, not skill package files.
