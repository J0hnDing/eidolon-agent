import json
import sys


BASE_SOLUTION = "534678912672195348198342567859761423426853791713924856961537284287419635345286179"
DIFFICULTY_REMOVALS = {
    "easy": 32,
    "medium": 43,
    "hard": 52,
}


def chunk_grid(value):
    return "\n".join(value[index:index + 9] for index in range(0, 81, 9))


def normalize_grid(value, field_name):
    if value is None:
        return None, []
    if not isinstance(value, str):
        return None, [f"{field_name} must be a string"]

    cleaned = "".join("." if char in {"0", "."} else char for char in value if not char.isspace())
    errors = []
    if len(cleaned) != 81:
        errors.append(f"{field_name} must contain exactly 81 cells")
    invalid = sorted({char for char in cleaned if char not in ".123456789"})
    if invalid:
        errors.append(f"{field_name} contains invalid characters: {''.join(invalid)}")
    return cleaned, errors


def units():
    rows = [[row * 9 + col for col in range(9)] for row in range(9)]
    cols = [[row * 9 + col for row in range(9)] for col in range(9)]
    boxes = []
    for box_row in range(3):
        for box_col in range(3):
            boxes.append([
                (box_row * 3 + row) * 9 + (box_col * 3 + col)
                for row in range(3)
                for col in range(3)
            ])
    return rows + cols + boxes


UNITS = units()


def rule_errors(grid, field_name):
    errors = []
    if not grid or len(grid) != 81:
        return errors

    for unit_index, unit in enumerate(UNITS):
        seen = {}
        for position in unit:
            digit = grid[position]
            if digit == ".":
                continue
            if digit in seen:
                group = "row" if unit_index < 9 else "column" if unit_index < 18 else "box"
                errors.append(f"{field_name} has duplicate {digit} in {group}")
                break
            seen[digit] = position
    return errors


def is_safe(grid, position, digit):
    row = position // 9
    col = position % 9
    for index in range(9):
        if grid[row * 9 + index] == digit:
            return False
        if grid[index * 9 + col] == digit:
            return False
    box_row = (row // 3) * 3
    box_col = (col // 3) * 3
    for r_offset in range(3):
        for c_offset in range(3):
            if grid[(box_row + r_offset) * 9 + box_col + c_offset] == digit:
                return False
    return True


def solve(grid):
    cells = list(grid)

    def backtrack():
        best_position = None
        best_candidates = None
        for position, value in enumerate(cells):
            if value != ".":
                continue
            candidates = [digit for digit in "123456789" if is_safe(cells, position, digit)]
            if not candidates:
                return False
            if best_candidates is None or len(candidates) < len(best_candidates):
                best_position = position
                best_candidates = candidates
        if best_position is None:
            return True
        for digit in best_candidates:
            cells[best_position] = digit
            if backtrack():
                return True
            cells[best_position] = "."
        return False

    if backtrack():
        return "".join(cells)
    return None


def make_puzzle(difficulty):
    removals = DIFFICULTY_REMOVALS.get(difficulty, DIFFICULTY_REMOVALS["medium"])
    cells = list(BASE_SOLUTION)
    order = [
        40, 0, 80, 8, 72, 4, 76, 36, 44, 20, 24, 56, 60, 2, 6, 74, 78,
        10, 16, 64, 70, 28, 34, 46, 52, 18, 26, 54, 62, 30, 32, 48, 50,
        12, 14, 66, 68, 22, 58, 38, 42, 1, 9, 71, 79, 3, 5, 75, 77,
        27, 35, 45, 53, 19, 25, 55, 61, 11, 15, 65, 69, 29, 33, 47, 51,
        21, 23, 57, 59, 37, 43, 13, 67, 31, 49, 39, 41, 7, 73, 17, 63,
    ]
    for position in order[:removals]:
        cells[position] = "."
    return "".join(cells)


def deterministic_hint(puzzle, solution=None):
    if not puzzle or len(puzzle) != 81:
        return None

    solved = solution or solve(puzzle)
    if not solved:
        return None

    for index, value in enumerate(puzzle):
        if value == ".":
            row = index // 9 + 1
            col = index % 9 + 1
            return {
                "row": row,
                "column": col,
                "value": solved[index],
                "message": f"Try {solved[index]} at row {row}, column {col}.",
            }
    return None


def base_result(message, errors=None):
    return {
        "puzzle": None,
        "grid": None,
        "difficulty": None,
        "is_valid": False,
        "is_complete": False,
        "errors": errors or [],
        "hint": None,
        "message": message,
    }


def generate(payload):
    difficulty = payload.get("difficulty", "medium")
    if difficulty not in DIFFICULTY_REMOVALS:
        difficulty = "medium"
    puzzle = make_puzzle(difficulty)
    include_hint = bool(payload.get("include_hint", False))
    return {
        "puzzle": puzzle,
        "grid": chunk_grid(puzzle),
        "difficulty": difficulty,
        "is_valid": True,
        "is_complete": False,
        "errors": [],
        "hint": deterministic_hint(puzzle, BASE_SOLUTION) if include_hint else None,
        "message": f"Generated a {difficulty} Sudoku puzzle.",
    }


def check(payload):
    puzzle, puzzle_errors = normalize_grid(payload.get("puzzle"), "puzzle")
    solution, solution_errors = normalize_grid(payload.get("solution"), "solution")
    errors = puzzle_errors + solution_errors
    if puzzle is None:
        errors.append("puzzle is required for check mode")
    if solution is None:
        errors.append("solution is required for check mode")
    if puzzle:
        errors.extend(rule_errors(puzzle, "puzzle"))
    if solution:
        errors.extend(rule_errors(solution, "solution"))

    if errors:
        result = base_result("The submitted Sudoku input is invalid.", errors)
        result["puzzle"] = puzzle
        result["grid"] = chunk_grid(solution or puzzle) if solution or puzzle else None
        result["hint"] = deterministic_hint(puzzle) if payload.get("include_hint") and puzzle else None
        return result

    incorrect_entries = []
    is_complete = "." not in solution
    is_valid = True
    for index, puzzle_value in enumerate(puzzle):
        solution_value = solution[index]
        if puzzle_value != "." and puzzle_value != solution_value:
            incorrect_entries.append({
                "row": index // 9 + 1,
                "column": index % 9 + 1,
                "expected": puzzle_value,
                "actual": solution_value,
            })
    solved = solve(puzzle)
    if solved:
        for index, value in enumerate(solution):
            if value != "." and value != solved[index]:
                entry = {
                    "row": index // 9 + 1,
                    "column": index % 9 + 1,
                    "expected": solved[index],
                    "actual": value,
                }
                if entry not in incorrect_entries:
                    incorrect_entries.append(entry)
    elif is_complete:
        is_valid = False
        errors.append("puzzle has no valid solution")

    if incorrect_entries:
        is_valid = False
        errors.append("solution contains incorrect entries")

    if "." in solution:
        is_valid = False

    message = "Solution is complete and valid." if is_valid and is_complete else "Solution is not complete or contains errors."
    result = {
        "puzzle": puzzle,
        "grid": chunk_grid(solution),
        "difficulty": payload.get("difficulty"),
        "is_valid": is_valid,
        "is_complete": is_complete,
        "errors": errors,
        "hint": deterministic_hint(puzzle, solved) if payload.get("include_hint") else None,
        "message": message,
    }

    if incorrect_entries and "incorrect_entries" in payload:
        result["incorrect_entries"] = incorrect_entries
    return result


def handle(payload):
    if not isinstance(payload, dict):
        return base_result("Input must be a JSON object.", ["input must be a JSON object"])

    mode = payload.get("mode", "generate")
    if mode == "generate":
        return generate(payload)
    if mode == "check":
        return check(payload)
    return base_result("Unsupported mode.", ["mode must be generate or check"])


def main():
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        result = handle(payload)
    except Exception as exc:
        result = base_result("Skill failed to process input.", [str(exc)])
    sys.stdout.write(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
