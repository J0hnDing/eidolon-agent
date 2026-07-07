# Project Build Workflow

New skill builds use a directed acyclic graph of task nodes instead of a linear milestone list. The graph is still specific to this application: it builds one proposed local skill under controlled folders, uses the same ProductManagerAgent, BuilderAgent, and TesterAgent roles, and keeps permission review in deterministic backend logic.

## Build Artifacts

The backend writes build artifacts under:

```text
runtime/agent_runs/run_<id>/
  intent_prompt.json
  decision.json
  blueprint.json
  permissions.json
  task_dag.json
  tasks/
    <task_id>.json
    <task_id>/
      interface_artifact.json
      failure.log
      test_result.json
  final_e2e_test_result.json
```

`task_dag.json` replaces `milestones/*.json`. The file name uses lowercase repository naming even when UI copy calls it the task DAG.

## ProductManager Actions

Every ProductManager action is a distinct backend-invoked step with a structured output.

1. `pm_refine_intent`
   - Inputs: latest Project-mode user message, prior clarification turns for the same generation request, and explicit user memory facts selected by the backend.
   - Output: `intent_prompt.json`.
   - Purpose: rewrite the request into a clearer build prompt for downstream PM actions.
   - Must not create a blueprint, permissions, task graph, or generated skill files.

2. `pm_review_plausibility`
   - Inputs: `intent_prompt.json`, original user request, and clarification history.
   - Output: `decision.json`.
   - Decisions:
     - `stop_inplausible`: the request is infeasible, unsafe, unsupported, or not a reusable local skill. The output includes the user-facing chat response and the workflow stops before artifact creation.
     - `ask_user_for_input`: the request needs clarification. The output includes one user-facing clarification question. The next Project-mode reply is appended to the same generation request and the backend repeats `pm_refine_intent` and `pm_review_plausibility`.
     - `proceed_to_blueprint`: the request is plausible and clear enough to continue.
   - Must not create blueprint, permission, or DAG artifacts.

3. `pm_write_blueprint`
   - Inputs: `intent_prompt.json`, `decision.json`, original user request, and relevant project constraints.
   - Output: `blueprint.json`.
   - Purpose: describe the skill goal, skill type, interface type, expected user behavior, package expectations, and high-level acceptance criteria.
   - Must not include task nodes, dependencies between tasks, or test files.

4. `pm_write_permissions`
   - Inputs: `intent_prompt.json`, `blueprint.json`, and project permission rules.
   - Output: `permissions.json`.
   - Purpose: draft both build-time needs and expected runtime permissions/dependencies.
   - Must include unsupported or blocked items as blocked instead of requesting approval for them.
   - Must not approve permissions.

5. Backend deterministic permission review
   - Inputs: `blueprint.json`, `permissions.json`, and permission policy.
   - Output: build-time approval request in chat.
   - Purpose: ask the user to approve Codex generation with a summary of the blueprint and build/runtime permission intent.
   - Approval to generate does not install, run, schedule, or approve runtime permissions.

6. `pm_write_task_dag`
   - Runs only after build-time approval.
   - Inputs: `intent_prompt.json`, `blueprint.json`, `permissions.json`, and approval result.
   - Output: `task_dag.json`.
   - Purpose: split the project into explicit task nodes with dependencies, difficulty, test requirements, I/O expectations, file write claims, and interface artifact expectations.
   - Must not include separate "test-only" task nodes. Tester actions are attached to the build nodes that require tests.

## Task DAG Schema

`task_dag.json` has this shape:

```json
{
  "schema_version": 1,
  "graph_id": "safe_skill_name_build",
  "root_task_ids": ["manifest_contract"],
  "nodes": [
    {
      "id": "manifest_contract",
      "title": "Manifest and package contract",
      "summary": "Create the manifest and README contract for the proposed skill.",
      "depends_on": [],
      "difficulty": "easy",
      "requires_tests": true,
      "parallel_safe": true,
      "expected_inputs": [
        "blueprint.json",
        "permissions.json"
      ],
      "parent_interface_artifacts": [],
      "expected_output_paths": [
        "manifest.json",
        "README.md"
      ],
      "file_write_claims": [
        "manifest.json",
        "README.md"
      ],
      "acceptance_criteria": [
        "manifest.json validates against the skill manifest schema",
        "README.md explains the local skill behavior"
      ],
      "test_expectations": [
        "validate manifest core fields and permission shape"
      ],
      "interface_artifact_expectations": [
        "declare manifest fields created",
        "declare schemas or entrypoints exposed to child tasks"
      ]
    }
  ],
  "edges": [
    {
      "from": "manifest_contract",
      "to": "entrypoint_behavior",
      "reason": "entrypoint task needs the manifest entrypoint contract"
    }
  ],
  "final_e2e_expectations": [
    "the generated package satisfies the blueprint end to end",
    "runtime manifest permissions match or narrow the approved plan"
  ]
}
```

Backend validation must reject the graph when:

- the graph is cyclic;
- `root_task_ids` is empty;
- a dependency references a missing node;
- a node id is not a safe path segment;
- a node omits acceptance criteria or expected output paths;
- an executable or hybrid skill has no tested node;
- two simultaneously ready nodes have overlapping `file_write_claims` without an explicit dependency ordering them.

The `file_write_claims` field is added so the backend can parallelize independent nodes without allowing two builders to edit the same generated file at the same time.

## Task Node Execution

After approval and DAG validation, the backend builds the DAG data structure and repeatedly executes ready nodes:

1. A node is ready when all `depends_on` nodes are `done`.
2. The backend may run multiple ready nodes in parallel when their `file_write_claims` do not overlap.
3. Each active node creates one Builder step.
4. If `requires_tests` is true, the Builder step is followed immediately by a Tester step for the same node.
5. A node is `done` only after Builder succeeds, its `interface_artifact.json` is written and valid, and required tests pass.
6. A node with `requires_tests = false` is still subject to deterministic package validation before it can be marked `done`.

Node statuses:

```text
pending, ready, building, testing, fixing, done, failed, blocked
```

## Builder Actions

### `builder_build_task`

Inputs:

- `blueprint.json`;
- `permissions.json`;
- `task_dag.json`;
- the current node file `tasks/<task_id>.json`;
- all direct and transitive parent `interface_artifact.json` files;
- existing generated files needed by the current node.

Behavior:

- build only the current task node;
- write only inside the controlled proposed skill folder;
- respect `file_write_claims` unless the backend grants an explicit serialized exception;
- produce or update skill package files for the node;
- write `runtime/agent_runs/run_<id>/tasks/<task_id>/interface_artifact.json`.

Interface artifact shape:

```json
{
  "task_id": "entrypoint_behavior",
  "created_paths": ["skill.py"],
  "updated_paths": ["manifest.json"],
  "interfaces": {
    "entrypoint": "skill.py",
    "input_schema": {},
    "output_schema": {}
  },
  "contracts_for_children": [
    "skill.py reads one JSON object from stdin and writes one JSON object to stdout"
  ],
  "known_limitations": []
}
```

### `builder_fix_task`

Inputs:

- all `builder_build_task` inputs for the node;
- node `failure.log`;
- failing test output;
- current generated files for the node.

Behavior:

- fix implementation files only;
- do not edit Tester-owned tests;
- preserve or narrow permissions unless the approved `permissions.json` explicitly allows the change;
- write a replacement `interface_artifact.json` if interfaces changed.

### `builder_fix_final_e2e`

Inputs:

- `blueprint.json`;
- `permissions.json`;
- `task_dag.json`;
- all task node files;
- all interface artifacts;
- final end-to-end failure output;
- all generated skill package files.

Behavior:

- repair cross-node integration issues;
- keep changes inside the controlled skill folder;
- do not edit final end-to-end tests;
- do not expand permissions without a new deterministic permission review.

## Tester Actions

### `tester_test_task`

Runs only for nodes with `requires_tests = true`.

Inputs:

- `blueprint.json`;
- `permissions.json`;
- the current task node;
- parent interface artifacts;
- Builder-created files for the node.

Behavior:

- write tests that match the node's `test_expectations` and acceptance criteria;
- write node-specific test files such as `tests/test_<task_id>.py` so parallel testers do not edit the same file;
- run tests through the safe validation path;
- write `tasks/<task_id>/test_result.json`;
- on failure, write `tasks/<task_id>/failure.log` and trigger `builder_fix_task`.

### `tester_final_e2e`

Runs after every task node is `done`.

Inputs:

- original user request;
- `intent_prompt.json`;
- `blueprint.json`;
- `permissions.json`;
- `task_dag.json`;
- all interface artifacts;
- all generated skill package files.

Behavior:

- write one final end-to-end test file, for example `tests/test_final_e2e.py`;
- validate the whole proposed skill against the blueprint and final DAG expectations;
- run the safe validation/test path;
- write `final_e2e_test_result.json`;
- on failure, trigger `builder_fix_final_e2e` and repeat until tests pass or the failure limit is reached.

## Failure Limits

Each task node has its own fix counter. After more than three failed test/fix attempts for one node, ProductManager writes a stuck user-facing summary and the run stops as `blocked`.

The final end-to-end loop has a separate failure counter. After more than three final failures, ProductManager writes a stuck user-facing summary and the run stops as `blocked`.

## Completion

After all task nodes and the final end-to-end test pass:

1. Backend validates the actual `manifest.json`.
2. Backend creates runtime permission review from the actual manifest and dependencies.
3. ProductManager does not write a completion summary unless the workflow is blocked or needs user input.
4. Chat tells the user only that the project is finished and surfaces any runtime permission approval needed before install.
5. The skill remains proposed until the user explicitly installs or rejects it.

## Project Repair Flow

Repair for an existing failed proposed skill or failed installed-skill copy may use a one-node DAG when the fix is local, or a small repair DAG when the issue spans multiple files.

Installed-skill repair must work on a proposed repair copy or draft version. It must never mutate the active installed version in place.

Repair uses the same task-node contracts:

- ProductManager writes a repair blueprint and repair task DAG;
- Builder repairs one task node at a time;
- Tester validates task nodes that require tests;
- Tester writes a final end-to-end test when all repair nodes pass;
- runtime permission review runs again if the manifest changes.

## Schema Changes From Linear Milestones

- `milestones/*.json` is replaced by `task_dag.json` and `tasks/<task_id>.json`.
- `build_next_milestone` is replaced by `proceed_to_blueprint` because the plausibility step no longer selects a linear next milestone.
- Per-node `requires_tests` controls whether Tester runs immediately after Builder.
- Per-node `file_write_claims` enables safe parallel Builder/Tester execution.
- Builder must write `interface_artifact.json` for every node so child nodes have explicit contracts instead of relying on hidden context.
- Tester writes node-specific test files and one final end-to-end test file instead of sharing one test file across all build work.
