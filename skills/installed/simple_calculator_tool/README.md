# Simple Calculator Tool

This proposed application skill is a local-only calculator tool for basic arithmetic. It is intentionally seeded with incorrect calculator behavior so later repair workflows can detect test failures and propose a fix before installation.

## Skill Type

- `skill_type`: `automation`
- `interface_type`: `tool`
- `risk_level`: `low`

## Input

The skill reads one JSON object from standard input:

```json
{
  "operation": "add",
  "left": 2,
  "right": 3
}
```

Supported operations are:

- `add`
- `subtract`
- `multiply`
- `divide`

## Output

The skill writes one JSON object to standard output:

```json
{
  "result": 5,
  "error": null
}
```

When an input or arithmetic error can be handled, the skill returns:

```json
{
  "result": null,
  "error": "Error message"
}
```

## Permissions

This skill requests no network access, no filesystem access, no secrets, and no shell execution.

## Known Intentional Bugs

The calculator implementation intentionally contains incorrect behavior for some operations. The included tests describe the correct expected behavior and should fail until a repair workflow fixes the implementation.

Do not install this proposed skill until validation and repair are complete.
