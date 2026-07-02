# Add Numbers

Add Numbers is a local automation skill that accepts two numeric inputs and returns their sum.

## Input

The skill reads a JSON object from standard input:

```json
{
  "a": 1,
  "b": 2
}
```

Both `a` and `b` must be numbers.

## Output

For valid input, the skill writes a JSON object to standard output:

```json
{
  "result": 3
}
```

For invalid input, the skill returns a JSON object with an `error` field and exits with a non-zero status code.

## Permissions

This skill does not request network access, filesystem access, secrets, shell access, dependencies, or a schedule.
