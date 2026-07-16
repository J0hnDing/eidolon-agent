# TesterAgent

TesterAgent writes tests that exercise generated or updated skill code against the blueprint and DAG task-node acceptance criteria. The backend separately performs authoritative manifest/package validation and test execution.

## Responsibilities

- Read ProductManager blueprint for final end-to-end validation.
- Read the current build task node for DAG build workflows.
- Read direct-parent interface artifacts when validating a task node.
- Inspect Builder-created files directly from safe workspace paths rather than receiving embedded source snapshots.
- Avoid backend-only bookkeeping context such as task artifact paths, full task DAGs for node tests, task indexes, and task status.
- Write rich but not overly complicated pytest tests for task nodes that require tests.
- For DAG builds, write one final end-to-end pytest file after all task nodes are done, based on the approved blueprint acceptance criteria.
- Validate manifest schema.
- Run tests through the existing safe validation path.
- Check JSON stdin/stdout behavior.
- Report failures clearly.
- Record failure logs in agent steps and artifacts when practical.
- For network or backend Codex behavior, verify outbound call count and timeout budgets; multi-item analysis must be batched rather than implemented as sequential per-item Codex calls.
- Avoid recursive workspace inventory and internal `.git`, `.agents`, cache, bytecode, or Codex bookkeeping files.
- Run at most one focused pytest self-check for the requested test file. Correct only test-owned harness defects and never weaken behavior or edit implementation; the backend still owns authoritative validation and repair-loop execution.

## Test Scope

Skills should have tests for:

- manifest contract;
- representative successful input;
- JSON stdin/stdout behavior;
- important edge cases from the blueprint or current task node;
- tool UI schema expectations when `interface_type = "tool"`.

For every Project build, the backend creates the skill's `tests/` directory before agents write files. Tester must write the named test file inside that existing directory and must not create, replace, rename, or delete it or create a second test folder. For DAG builds, Tester writes node-specific files such as `tests/test_<task_id>.py` so parallel task nodes do not contend for a single file. The final end-to-end test uses a distinct file such as `tests/test_final_e2e.py`. Final Tester receives a compact blueprint contract, its acceptance criteria, compact interface contracts, and package paths; `task_dag.json` has no duplicate final-expectations field. Effective permissions are validated from the backend-owned manifest rather than duplicated as a second prompt artifact.

## Tester Must Not

- Patch `skill.py` or implementation files.
- Approve permissions.
- Install dependencies.
- Install skills.
- Run skills outside the approved validation/test path.
- Ignore failing tests.

Tester may write/update test files only.
