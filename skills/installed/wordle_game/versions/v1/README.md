# Wordle Game

Wordle Game is a local Wordle-style automation tool. It runs a six-attempt guessing game where each guess must be exactly five alphabetic characters.

The tool is exposed through the declarative Tools UI described in `manifest.json`. The form contains:

- `Guess`: required text input for a five-letter guess, with `CRANE` as the placeholder.
- `Start New Game`: optional checkbox that clears the saved game state before applying the submitted guess.

## Inputs

- `guess`: A five-letter alphabetic word guess. Guesses are normalized to uppercase before feedback is produced.
- `new_game`: When `true`, the current cached game is reset and the submitted guess starts a fresh game. Defaults to `false`.

## Outputs

- `status`: Current game status. Values are `in_progress`, `won`, `lost`, `reset`, or `error`.
- `result`: Human-readable result message suitable for the primary tool result display.
- `attempts_used`: Number of guesses used in the current game, from 0 to 6.
- `attempts_remaining`: Number of guesses remaining in the current game, from 0 to 6.
- `feedback`: Per-letter feedback for the submitted guess. Each item includes a `letter` and a `state` of `correct`, `present`, or `absent`.
- `history`: Prior guesses and their feedback for the current game.

## Runtime State

Game state is stored only inside `./cache`. The cached state tracks the selected answer, terminal game status, and guess history so the next invocation can continue the same game.

The skill requests no network access, shell access, secrets, external dependencies, broad filesystem reads, or filesystem writes outside `./cache`.

## Maintenance

The executable entrypoint is `skill.py`. It reads a JSON object from stdin, writes a JSON object to stdout, and avoids side effects on import.

Expected validation includes manifest inspection and the TesterAgent-owned test command:

```powershell
python -m pytest tests/test_skill.py
```

This proposed skill package does not claim that the skill is installed, enabled, approved, or already run.
