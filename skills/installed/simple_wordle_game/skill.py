"""Local-only Wordle-style automation skill.

The executable contract is JSON in on stdin and JSON out on stdout.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from typing import Any


MAX_ATTEMPTS = 6
WORD_LENGTH = 5
ANSWER = "burnt"

ANSWER_LIST = {
    "burnt",
    "crane",
    "slate",
    "flint",
    "proud",
    "ghost",
    "spore",
    "light",
    "brick",
    "sugar",
    "plant",
    "world",
    "vivid",
    "mirth",
    "sound",
}


def make_response(
    status: str,
    attempts_used: int,
    rows: list[dict[str, Any]],
    message: str,
) -> dict[str, Any]:
    return {
        "status": status,
        "attempts_used": attempts_used,
        "max_attempts": MAX_ATTEMPTS,
        "rows": rows,
        "message": message,
    }


def invalid_response(message: str, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return make_response("invalid", len(rows or []), rows or [], message)


def normalize_guesses(raw_guesses: Any) -> tuple[list[str] | None, str | None]:
    if isinstance(raw_guesses, str):
        guesses = [line.strip() for line in raw_guesses.splitlines() if line.strip()]
    elif isinstance(raw_guesses, list):
        guesses = raw_guesses
    else:
        return None, "guesses must be an array of five-letter strings."

    if not 1 <= len(guesses) <= MAX_ATTEMPTS:
        return None, "guesses must contain between 1 and 6 entries."

    normalized: list[str] = []
    for index, guess in enumerate(guesses, start=1):
        if not isinstance(guess, str):
            return None, f"guess {index} must be a string."

        word = guess.strip().lower()
        if len(word) != WORD_LENGTH or not word.isalpha():
            return None, f"guess {index} must be exactly five letters."

        if word not in ANSWER_LIST:
            return None, f"guess {index} is not in the embedded word list."

        normalized.append(word)

    return normalized, None


def score_guess(guess: str, answer: str = ANSWER) -> list[str]:
    feedback = ["absent"] * WORD_LENGTH
    remaining = Counter()

    for index, letter in enumerate(guess):
        if letter == answer[index]:
            feedback[index] = "correct"
        else:
            remaining[answer[index]] += 1

    for index, letter in enumerate(guess):
        if feedback[index] == "correct":
            continue
        if remaining[letter] > 0:
            feedback[index] = "present"
            remaining[letter] -= 1

    return feedback


def play_wordle(payload: dict[str, Any]) -> dict[str, Any]:
    guesses, error = normalize_guesses(payload.get("guesses"))
    if error:
        return invalid_response(error)

    rows: list[dict[str, Any]] = []
    for guess in guesses or []:
        feedback = score_guess(guess)
        rows.append({"guess": guess, "feedback": feedback})
        if guess == ANSWER:
            return make_response("won", len(rows), rows, "You solved it.")

    if len(rows) >= MAX_ATTEMPTS:
        return make_response("lost", len(rows), rows, f"No guesses left. The answer was {ANSWER}.")

    return make_response("in_progress", len(rows), rows, "Keep guessing.")


def load_payload(stdin_text: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(stdin_text or "{}")
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON input: {exc.msg}."

    if not isinstance(payload, dict):
        return None, "input must be a JSON object."

    allowed_keys = {"guesses"}
    extra_keys = sorted(set(payload) - allowed_keys)
    if extra_keys:
        return None, f"unsupported input field: {extra_keys[0]}."

    if "guesses" not in payload:
        return None, "missing required field: guesses."

    return payload, None


def main() -> None:
    payload, error = load_payload(sys.stdin.read())
    if error:
        result = invalid_response(error)
    else:
        result = play_wordle(payload or {})

    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
