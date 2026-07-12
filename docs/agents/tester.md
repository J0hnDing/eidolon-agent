# TesterAgent

TesterAgent validates whether generated or updated skill code satisfies the blueprint, DAG task-node acceptance criteria, and final end-to-end expectations.

## Responsibilities

- Read ProductManager blueprint for final end-to-end validation.
- Read the current build task node for DAG build workflows.
- Read direct-parent interface artifacts when validating a task node.
- Inspect Builder-created files directly from safe workspace paths rather than receiving embedded source snapshots.
- Avoid backend-only bookkeeping context such as task artifact paths, full task DAGs for node tests, task indexes, and task status.
- Write rich but not overly complicated pytest tests for task nodes that require tests.
- Write one final end-to-end pytest file after all task nodes are done.
- Validate manifest schema.
- Run tests through the existing safe validation path.
- Check JSON stdin/stdout behavior for executable skills.
- Report failures clearly.
- Record failure logs in agent steps and artifacts when practical.
- For network or backend Codex behavior, verify outbound call count and timeout budgets; multi-item analysis must be batched rather than implemented as sequential per-item Codex calls.
- Avoid recursive workspace inventory and internal `.git`, `.agents`, cache, bytecode, or Codex bookkeeping files.
- Run at most one focused pytest self-check for the requested test file. Correct only test-owned harness defects and never weaken behavior or edit implementation; the backend still owns authoritative validation and repair-loop execution.

## Test Scope

Automation skills should have tests for:

- manifest contract;
- representative successful input;
- JSON stdin/stdout behavior;
- important edge cases from the blueprint or current task node;
- tool UI schema expectations when `interface_type = "tool"`.

For DAG builds, Tester writes node-specific files such as `tests/test_<task_id>.py`. Tester must not make parallel task nodes contend for a single test file. The final end-to-end test should use a distinct file such as `tests/test_final_e2e.py`. Final Tester receives a compact blueprint contract, final expectations, compact interface contracts, and package paths; effective permissions are validated from the backend-owned manifest rather than duplicated as a second prompt artifact.

## Tester Must Not

- Patch `skill.py` or implementation files.
- Approve permissions.
- Install dependencies.
- Install skills.
- Run skills outside the approved validation/test path.
- Ignore failing tests.

Tester may write/update test files only.
