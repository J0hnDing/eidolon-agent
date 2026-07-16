# Tools

A tool is a user-facing manual interface for an installed runnable skill.

## Definition

A skill appears in Tools when:

- status is `installed`;
- enabled is true;
- `interface_type` is `tool`.

## Tool UI Schema

Tools may provide declarative `tool_ui_schema` in `manifest.json`. The frontend safely renders supported field types:

```text
text, number, textarea, checkbox, select
```

Generated skills must not provide React, HTML, JavaScript, or app source code for tool UIs.

## Running Tools

`POST /tools/{skill_id}/run` uses the same safe runner path as manual skill runs. It enforces installed/enabled status, runtime approval, supported permissions, and operation locks.

## Fallback

If no usable `tool_ui_schema` exists, Tool Detail falls back to a JSON editor.
