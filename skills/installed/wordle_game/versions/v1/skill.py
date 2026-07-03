import hashlib
import json
import sys
from collections import Counter
from pathlib import Path


MAX_ATTEMPTS = 6
DEFAULT_SESSION_ID = "default"
CACHE_DIR = Path(__file__).resolve().parent / "cache"

ANSWERS = [
    "APPLE",
    "BRAIN",
    "CHAIR",
    "DELTA",
    "EAGER",
    "FLAME",
    "GRACE",
    "HOUSE",
    "INDEX",
    "JELLY",
    "KNIFE",
    "LEMON",
    "MANGO",
    "NERVE",
    "OCEAN",
    "PLANT",
    "QUEEN",
    "ROAST",
    "STONE",
    "TRAIN",
    "UNION",
    "VIVID",
    "WATER",
    "YEAST",
    "ZEBRA"
]


def normalize_session_id(value):
    if value is None:
        return DEFAULT_SESSION_ID
    session_id = str(value).strip()
    return session_id or DEFAULT_SESSION_ID


def session_path(session_id):
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.json"


def choose_answer(session_id, game_number):
    seed = f"{session_id}:{game_number}".encode("utf-8")
    digest = hashlib.sha256(seed).hexdigest()
    index = int(digest, 16) % len(ANSWERS)
    return ANSWERS[index]


def new_state(session_id, game_number=1):
    return {
        "session_id": session_id,
        "game_number": game_number,
        "answer": choose_answer(session_id, game_number),
        "attempts": [],
        "status": "in_progress"
    }


def load_state(session_id):
    path = session_path(session_id)
    if not path.exists():
        return new_state(session_id)

    try:
        with path.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return new_state(session_id)

    if not isinstance(state, dict):
        return new_state(session_id)

    answer = state.get("answer")
    attempts = state.get("attempts")
    status = state.get("status")
    game_number = state.get("game_number", 1)

    if (
        not isinstance(answer, str)
        or len(answer) != 5
        or not answer.isalpha()
        or not isinstance(attempts, list)
        or status not in {"in_progress", "won", "lost"}
        or not isinstance(game_number, int)
    ):
        return new_state(session_id)

    return {
        "session_id": session_id,
        "game_number": max(1, game_number),
        "answer": answer.upper(),
        "attempts": [str(item).upper() for item in attempts if isinstance(item, str)],
        "status": status
    }


def save_state(state):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = session_path(state["session_id"])
    with path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)


def score_guess(guess, answer):
    statuses = ["absent"] * 5
    remaining = Counter()

    for index, letter in enumerate(guess):
        if letter == answer[index]:
            statuses[index] = "correct"
        else:
            remaining[answer[index]] += 1

    for index, letter in enumerate(guess):
        if statuses[index] == "correct":
            continue
        if remaining[letter] > 0:
            statuses[index] = "present"
            remaining[letter] -= 1

    return [
        {
            "letter": letter,
            "status": statuses[index]
        }
        for index, letter in enumerate(guess)
    ]


def visible_answer(state):
    if state["status"] in {"won", "lost"}:
        return state["answer"]
    return None


def response(result, session_id, guess, feedback, state):
    attempts_used = len(state["attempts"])
    return {
        "result": result,
        "session_id": session_id,
        "guess": guess,
        "feedback": feedback,
        "attempts_used": attempts_used,
        "attempts_remaining": max(0, MAX_ATTEMPTS - attempts_used),
        "game_status": state["status"],
        "answer": visible_answer(state)
    }


def validate_input(payload):
    if not isinstance(payload, dict):
        raise ValueError("Input must be a JSON object.")
    if "guess" not in payload:
        raise ValueError("Missing required field: guess.")

    guess = payload["guess"]
    if not isinstance(guess, str):
        raise ValueError("Guess must be a string.")

    guess = guess.strip().upper()
    if len(guess) != 5 or not guess.isalpha():
        raise ValueError("Guess must contain exactly five alphabetic characters.")

    return guess


def play(payload):
    session_id = normalize_session_id(payload.get("session_id"))
    guess = validate_input(payload)
    state = load_state(session_id)

    if bool(payload.get("new_game")):
        state = new_state(session_id, state.get("game_number", 1) + 1)

    if state["status"] != "in_progress":
        return response(
            f"This game is already {state['status']}. Start a new game to keep playing.",
            session_id,
            guess,
            [],
            state
        )

    feedback = score_guess(guess, state["answer"])
    state["attempts"].append(guess)

    if guess == state["answer"]:
        state["status"] = "won"
        result = f"Correct. You solved the puzzle in {len(state['attempts'])} attempt(s)."
    elif len(state["attempts"]) >= MAX_ATTEMPTS:
        state["status"] = "lost"
        result = f"No attempts remaining. The answer was {state['answer']}."
    else:
        remaining = MAX_ATTEMPTS - len(state["attempts"])
        result = f"Guess recorded. {remaining} attempt(s) remaining."

    save_state(state)
    return response(result, session_id, guess, feedback, state)


def error_response(message):
    return {
        "result": message,
        "session_id": DEFAULT_SESSION_ID,
        "guess": "",
        "feedback": [],
        "attempts_used": 0,
        "attempts_remaining": MAX_ATTEMPTS,
        "game_status": "in_progress",
        "answer": None
    }


def main():
    try:
        raw_input = sys.stdin.read()
        payload = json.loads(raw_input)
        output = play(payload)
    except json.JSONDecodeError:
        output = error_response("Input must be valid JSON.")
    except ValueError as exc:
        output = error_response(str(exc))
    except Exception as exc:
        output = error_response(f"Unexpected error: {exc}")

    sys.stdout.write(json.dumps(output, separators=(",", ":")))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
