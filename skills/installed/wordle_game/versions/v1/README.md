# Wordle Game

Wordle Game is a local tool skill for playing a five-letter word guessing game. It reads one JSON object from stdin and writes one JSON object to stdout.

## Inputs

- `guess` is required and must be exactly five alphabetic characters.
- `session_id` is optional. Use the same value to continue a specific game. Blank or omitted values use the `default` session.
- `new_game` is optional. Set it to `true` to reset the selected session before applying the submitted guess.

Example input:

```json
{
  "guess": "crane",
  "session_id": "default",
  "new_game": false
}
```

## Outputs

The skill returns:

- `result`: a short outcome summary.
- `session_id`: the session used for the game.
- `guess`: the normalized uppercase guess.
- `feedback`: five letter results using `correct`, `present`, or `absent`.
- `attempts_used`: number of submitted guesses in the current session.
- `attempts_remaining`: number of guesses left.
- `game_status`: `in_progress`, `won`, or `lost`.
- `answer`: `null` while the game is in progress, then the answer after a win or loss.

## Game Rules

Each session allows six attempts. Feedback follows Wordle-style duplicate-letter limits: exact-position matches are marked first, then present letters are counted only while unmatched copies remain in the answer.

After a session is won or lost, additional guesses do not change that session. Start a new puzzle by submitting `new_game: true`.

## Local State and Permissions

Session state is stored only in `./cache` inside this skill folder. The skill does not use network access, secrets, shell access, external dependencies, or filesystem access outside its own cache directory.
