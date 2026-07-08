import json
import random
import sys
from pathlib import Path


MAX_ATTEMPTS = 6
CACHE_DIR = Path(__file__).resolve().parent / "cache"
STATE_FILE_NAME = "game_state.json"

ANSWER_WORDS = [
    "CRANE",
    "SLATE",
    "PLANT",
    "BRICK",
    "CLOUD",
    "SHINE",
    "GRAPE",
    "MIRTH",
    "BLOOM",
    "FROST",
    "CHAIR",
    "WATER",
    "LIGHT",
    "STONE",
    "PRIDE",
    "FLAME",
    "SOUND",
    "TRAIL",
    "HONEY",
    "SPICE",
]


def _state_file() -> Path:
    return CACHE_DIR / STATE_FILE_NAME


def _empty_response(status: str, result: str) -> dict:
    return {
        "status": status,
        "result": result,
        "attempts_used": 0,
        "attempts_remaining": MAX_ATTEMPTS,
        "feedback": [],
        "history": [],
    }


def _error_response(message: str, state: dict | None = None) -> dict:
    history = list(state.get("history", [])) if isinstance(state, dict) else []
    attempts_used = min(len(history), MAX_ATTEMPTS)
    return {
        "status": "error",
        "result": message,
        "attempts_used": attempts_used,
        "attempts_remaining": max(MAX_ATTEMPTS - attempts_used, 0),
        "feedback": [],
        "history": history,
    }


def _choose_answer() -> str:
    return random.choice(ANSWER_WORDS)


def _new_state() -> dict:
    return {
        "answer": _choose_answer(),
        "history": [],
        "terminal": False,
        "status": "in_progress",
    }


def _load_state() -> dict | None:
    path = _state_file()
    if not path.exists():
        return None

    try:
        with path.open("r", encoding="utf-8") as state_file:
            state = json.load(state_file)
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(state, dict):
        return None
    answer = state.get("answer")
    history = state.get("history")
    if not isinstance(answer, str) or len(answer) != 5 or not answer.isalpha():
        return None
    if not isinstance(history, list):
        return None

    normalized_history = []
    for item in history[:MAX_ATTEMPTS]:
        if not isinstance(item, dict):
            continue
        guess = item.get("guess")
        feedback = item.get("feedback")
        if isinstance(guess, str) and isinstance(feedback, list):
            normalized_history.append(
                {
                    "guess": guess.upper(),
                    "feedback": feedback,
                }
            )

    return {
        "answer": answer.upper(),
        "history": normalized_history,
        "terminal": bool(state.get("terminal", False)),
        "status": state.get("status", "in_progress"),
    }


def _save_state(state: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with _state_file().open("w", encoding="utf-8") as state_file:
        json.dump(state, state_file, indent=2, sort_keys=True)


def _validate_guess(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    guess = value.strip().upper()
    if len(guess) != 5 or not guess.isalpha():
        return None
    return guess


def _build_feedback(guess: str, answer: str) -> list[dict]:
    states = ["absent"] * len(guess)
    remaining_letters: dict[str, int] = {}

    for index, letter in enumerate(guess):
        if letter == answer[index]:
            states[index] = "correct"
        else:
            answer_letter = answer[index]
            remaining_letters[answer_letter] = remaining_letters.get(answer_letter, 0) + 1

    for index, letter in enumerate(guess):
        if states[index] == "correct":
            continue
        if remaining_letters.get(letter, 0) > 0:
            states[index] = "present"
            remaining_letters[letter] -= 1

    return [
        {
            "letter": letter,
            "state": states[index],
        }
        for index, letter in enumerate(guess)
    ]


def _result_message(status: str, guess: str, attempts_used: int, answer: str) -> str:
    if status == "won":
        return f"Correct: {guess}. You solved it in {attempts_used} attempt(s)."
    if status == "lost":
        return f"Game over. The answer was {answer}."
    return f"Guess accepted: {guess}. {MAX_ATTEMPTS - attempts_used} attempt(s) remaining."


def run(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return _empty_response("error", "Input must be a JSON object.")

    state = _load_state()
    if payload.get("new_game") is True:
        state = _new_state()
    elif state is None or state.get("terminal") is True:
        state = _new_state()

    guess = _validate_guess(payload.get("guess"))
    if guess is None:
        return _error_response("Guess must be exactly five alphabetic characters.", state)

    answer = state["answer"]
    feedback = _build_feedback(guess, answer)
    history_item = {
        "guess": guess,
        "feedback": feedback,
    }
    history = list(state.get("history", []))
    history.append(history_item)
    attempts_used = min(len(history), MAX_ATTEMPTS)
    attempts_remaining = max(MAX_ATTEMPTS - attempts_used, 0)

    if guess == answer:
        status = "won"
        terminal = True
    elif attempts_used >= MAX_ATTEMPTS:
        status = "lost"
        terminal = True
    else:
        status = "in_progress"
        terminal = False

    state.update(
        {
            "history": history,
            "terminal": terminal,
            "status": status,
        }
    )
    _save_state(state)

    return {
        "status": status,
        "result": _result_message(status, guess, attempts_used, answer),
        "attempts_used": attempts_used,
        "attempts_remaining": attempts_remaining,
        "feedback": feedback,
        "history": history,
    }


def main() -> int:
    try:
        raw_input = sys.stdin.read()
        payload = json.loads(raw_input) if raw_input.strip() else {}
        response = run(payload)
    except Exception as exc:
        response = _empty_response("error", f"Unable to process input: {exc}")

    print(json.dumps(response, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
