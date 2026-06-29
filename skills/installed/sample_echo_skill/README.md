# sample_echo_skill

Sample `automation` skill created by the local proposed skill workflow.

## Input

Send a JSON object. The friendliest input is:

```json
{
  "message": "Hello, assistant",
  "label": "Manual test"
}
```

`message` is echoed back. `label` is optional and is used in the summary.

## Output

The skill returns readable JSON:

```json
{
  "title": "Sample Echo Skill",
  "summary": "Manual test: Hello, assistant",
  "echoed_message": "Hello, assistant",
  "received_input": {
    "message": "Hello, assistant",
    "label": "Manual test"
  },
  "suggested_next_input": {
    "message": "Try editing this message and running again."
  },
  "warnings": []
}
```
