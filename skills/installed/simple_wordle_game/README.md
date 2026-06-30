# Simple Wordle Game

Simple Wordle Game is a local-only automation skill for playing a standard six-guess, five-letter Wordle-style game.

The skill reads a JSON object from stdin and writes a JSON object to stdout. It does not use network access, secrets, shell execution, external dependencies, or filesystem permissions.

## Input

```json
{
  "guesses": ["crane", "slate"]
}
```

`guesses` must contain one to six five-letter words. The tool UI may collect guesses as one per line, but the executable contract expects a JSON array.

## Output

```json
{
  "status": "in_progress",
  "attempts_used": 2,
  "max_attempts": 6,
  "rows": [
    {
      "guess": "crane",
      "feedback": ["absent", "present", "absent", "correct", "absent"]
    }
  ],
  "message": "Keep guessing."
}
```

Feedback values:

- `correct`: the letter is in the answer at that position.
- `present`: the letter exists in the answer at another position.
- `absent`: the letter is not available in the answer after correct and present letters are counted.

## Game Rules

- The embedded answer for this MVP skill is `burnt`.
- Guesses are validated against the embedded word list.
- The game is won when a guess exactly matches the answer.
- The game is lost after six valid guesses without finding the answer.
- Invalid input returns a JSON response with `status` set to `invalid`.

## Permissions

This skill requests only low-risk local execution permissions:

```json
{
  "network": [],
  "filesystem_read": [],
  "filesystem_write": [],
  "secrets": [],
  "shell": false
}
```
