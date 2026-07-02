# Sudoku Game

Sudoku Game is a local automation skill that generates playable Sudoku puzzles, checks completed grids, and returns one optional next-cell hint. It uses only Python standard-library logic and does not request network, filesystem, secret, or shell permissions.

## Input

The skill reads a JSON object from standard input.

```json
{
  "mode": "generate",
  "difficulty": "medium",
  "include_hint": true
}
```

For checking a completed puzzle:

```json
{
  "mode": "check",
  "puzzle": "530070000600195000098000060800060003400803001700020006060000280000419005000080079",
  "solution": "534678912672195348198342567859761423426853791713924856961537284287419635345286179",
  "include_hint": false
}
```

`puzzle` must be 81 characters using digits `1`-`9` and either `0` or `.` for blanks. `solution` must be 81 digits from `1` to `9`.

## Output

The skill writes a single JSON object to standard output with:

- `puzzle`: the 81-character puzzle string, when available
- `grid`: a readable 9x9 grid, when available
- `difficulty`: the selected difficulty, when available
- `is_valid`: whether the submitted puzzle or solution is valid
- `is_complete`: whether the submitted or generated grid is complete
- `errors`: validation errors, if any
- `hint`: a safe next-cell hint, when requested and possible
- `message`: a short user-facing result

## Permissions

This skill is local-only:

- no network access
- no filesystem reads or writes
- no secrets
- no shell access
- no package dependencies

