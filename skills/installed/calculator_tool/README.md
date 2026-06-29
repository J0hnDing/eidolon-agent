# calculator_tool

Installed demo tool skill for basic local arithmetic.

## Input

```json
{
  "expression": "1+2*3"
}
```

## Output

```json
{
  "result": 7
}
```

The skill parses arithmetic with Python `ast` and allows only numeric constants and arithmetic operators. It does not use `eval`.
