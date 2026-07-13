# Project Build Workflow

New skill builds share one general starting sequence and then dispatch to a backend-managed build workflow. ProductManager intent refinement, plausibility review, blueprint creation, permission planning, and deterministic build-time approval happen before workflow dispatch. The supported build workflows are `single_codex` and `task_dag`.

## Build Artifacts

The backend writes build artifacts under:

```text
runtime/agent_runs/run_<id>/
  intent_prompt.json
  decision.json
  blueprint.json
  permissions.json
  task_dag.json              task_dag only
  tasks/                     task_dag only
    <task_id>.json
    <task_id>/
      interface_artifact.json
      failure.log
      test_result.json
  final_e2e_test_result.json
```

`task_dag.json` replaces `milestones/*.json` for the `task_dag` workflow. The `single_codex` workflow does not create DAG or task artifacts.

## ProductManager Actions

Every ProductManager action is a distinct backend-invoked step with a structured output.
The actuall input to Codex_cli also includes corresponding instructions.
Output for PM does NOT mean agent writes files directly, instead backend receives the response in json and writes it.

1. `pm_refine_intent`
   - Inputs: latest Project-mode user message, prior clarification turns for the same generation request, and explicit user memory facts, this is currently default to none until memory system implemented.
   - Output: `intent_prompt.json`.
   - Purpose: rewrite the request into a clearer build prompt for downstream PM actions.
   - ProductManager refinement runs after every Project-mode user input, including a first-turn request with no clarification history or selected memory facts.
   - Only `schema_version` and `refined_prompt` continue to plausibility and blueprint prompts. Selected memory facts remain in the stored intent artifact for audit and are not repeated downstream.
   - Must not create a blueprint, permissions, task graph, or generated skill files.

2. `pm_review_plausibility`
   - Inputs: `intent_prompt.json`.
   - Output: `decision.json`.
   - Decisions:
     - `stop_inplausible`: the request is infeasible, unsafe, unsupported, or not a reusable local skill. The output includes the user-facing chat response and the workflow stops before artifact creation.
     - `ask_user_for_input`: the request needs clarification. The output includes one user-facing clarification question. The next Project-mode reply is appended to the same generation request and the backend repeats `pm_refine_intent` and `pm_review_plausibility`.
     - `proceed_to_blueprint`: the request is plausible and clear enough to continue.
   - Must not create blueprint, permission, or DAG artifacts.

3. `pm_write_blueprint_and_permissions`
   - Inputs: `intent_prompt.json`.
   - Output: one structured response containing top-level `build_workflow`, `blueprint`, and `permission_plan` fields. The backend writes only the latter two as `blueprint.json` and `permissions.json`.
   - Purpose: choose `single_codex` or `task_dag`; describe the skill goal, skill type, interface type, expected user behavior, schedule intent, and high-level acceptance criteria; draft both build-time needs and expected runtime permissions/dependencies.
   - `build_workflow` is backend routing state stored on the agent run. It must not appear inside `blueprint` or in `blueprint.json` because downstream DAG, Builder, and Tester inputs do not need it.
   - Must not enumerate generated package files. Builder-owned paths are defined later by task-node `expected_output_paths` and `file_write_claims` in `task_dag.json`.
   - Must not include task nodes, dependencies between tasks, or test files.
   - Must not approve permissions.

4. Backend deterministic permission review
   - Inputs: `blueprint.json`, `permissions.json`.
   - Output: if approval required, build-time approval request in chat, otherwise proceed automatically.
   - Purpose: Manage permissions safely.
   - Approval to generate does not install, run, schedule, or approve runtime permissions.
   - Also writes a `manifest.json` from `blueprint.json`, `permissions.json`.

5. Backend workflow dispatch
   - Inputs: the agent run's backend-only `build_workflow`, approved `blueprint.json`, and effective `permissions.json`.
   - Output: execution by the registered `single_codex` or `task_dag` workflow module.
   - The registry rejects unknown workflow names. Workflow selection and execution are not hard-coded into the common pre-approval sequence.

6. `pm_write_task_dag` (`task_dag` only)
   - Inputs: `blueprint.json`, compact backend-approved `permission_bounds`, and the static backend API index file at `backend/app/static/backend_api_index.json`.
   - Output: `task_dag.json`.
   - Purpose: split the project into explicit task nodes with dependencies, difficulty, test requirements, output expectations, file write claims, interface artifact expectations, and any required backend API ids.
   - Must not include separate "test-only" task nodes. Tester actions are attached to the build nodes that require tests.

## Single-Codex Workflow

The `single_codex` workflow package contains `workflow.py`, `prompts.py`, and `instructions/run.md`. It makes one writable Codex invocation in the controlled proposed-skill folder. Its prompt contains the approved blueprint, effective permissions, and fixed workflow instructions. Within that invocation Codex plans internally, creates the complete skill package, writes tests for automation skills, runs a focused test command, and fixes failures before returning.

The backend still seeds and finalizes `manifest.json`, records invocation usage, creates runtime permission review from the actual manifest, and leaves the skill proposed. It does not currently run an independent final proposed-package validation after the single Codex invocation. Adding that validation is tracked in `docs/todo.md`.

The workflow can pause before its single invocation when Codex allowance is below the configured reserve. It cannot pause partway through the invocation; a retry starts a new Codex invocation against a freshly prepared proposed-skill workspace.

## Task DAG Workflow

The `task_dag` package contains its executor, prompt composition, and ProductManager/Builder/Tester/repair instructions. It retains the existing ProductManager, BuilderAgent, and TesterAgent orchestration and deterministic node/final validation behavior without placing workflow-specific prompt mappings in `CodexService` or the common `AgentWorkflowService` dispatch path.

### Task DAG Schema

`task_dag.json` has this shape:

```json
{
  "task_dag": {
    "schema_version": 1,
    "nodes": [
      {
        "id": "short_safe_id",
        "title": "short title",
        "summary": "specific work BuilderAgent should complete for this task node",
        "depends_on": [],
        "difficulty": "easy|medium|hard",
        "requires_tests": true,
        "parallel_safe": true,
        "expected_output_paths": ["skill.py"],
        "file_write_claims": ["skill.py"],
        "acceptance_criteria": ["string"],
        "test_expectations": ["string"],
        "interface_artifact_expectations": ["string"],
        "backend_api_ids": []
      }
    ],
    "final_e2e_expectations": ["string"]
  }
}
```

`requires_tests` and `parallel_safe` are JSON booleans. Root nodes are derived from an empty `depends_on` list. `manifest.json` is excluded from `file_write_claims`; the backend owns it and grants Builder a serialized exception when a task needs to update it.

Backend validation must reject the graph when:

- the graph is cyclic;
- a dependency references a missing node;
- a node id is not a safe path segment;
- a node omits acceptance criteria or expected output paths;
- a node references a backend API id that is not in the backend API catalog;
- an automation skill has no tested node;
- two simultaneously ready nodes have overlapping `file_write_claims` without an explicit dependency ordering them.

The `file_write_claims` field is added so the backend can parallelize independent nodes without allowing two builders to edit the same generated file at the same time.

Task `expected_output_paths` and `file_write_claims` are Builder-owned skill package paths only. They must not include Tester-owned files such as `tests/test_skill.py` or `tests/test_<task_id>.py`; the backend sanitizes those paths out of ProductManager DAG output before validation.

The blueprint does not define package file paths. ProductManager assigns Builder-owned paths directly to task nodes in `task_dag.json`; the backend does not invent omitted task outputs. `README.md` and `SKILL.md` are not universal package requirements; `manifest.json` remains backend-owned and is always validated, including any entrypoint or instructions file it declares.

### Task Node Execution

After approval and DAG validation, the backend builds the DAG data structure and repeatedly executes ready nodes:

1. Before the first Builder step, the backend derives a skeleton `manifest.json` in the proposed skill folder from `blueprint.json` and `permissions.json`.
2. A node is ready when all `depends_on` nodes are `done`.
3. The backend may run multiple ready nodes in parallel when their `file_write_claims` do not overlap.
4. Each active node creates one Builder step.
5. After each Builder step, the backend deterministically fills missing manifest fields from the approved blueprint and permission plan when possible.
6. If `requires_tests` is true, the Builder step is followed immediately by a Tester step for the same node.
7. A node is `done` only after Builder succeeds, the backend validates and moves its skill-local `interface_artifact.json` into the task's run-artifact folder, and required tests pass.
8. A node with `requires_tests = false` is still subject to deterministic package validation before it can be marked `done`.

Node statuses:

```text
pending, ready, building, testing, fixing, done, failed, blocked
```

The backend groups simultaneously ready `parallel_safe` nodes into execution batches. The current shared skill workspace processes nodes within an admitted batch deterministically, while the batch boundary preserves the same safety contract for a parallel executor: all nodes already admitted to the batch finish before a quota pause, and no node from the next ready batch starts.

Before admitting another ready batch and after an admitted batch finishes, the backend refreshes the Codex account allowance. If either the 5-hour or weekly window has less than 5% remaining, the agent run becomes `paused`. Exactly 5% remains runnable. The workflow page shows the reset time and a Resume control. Resume rechecks both windows and continues from persisted `done` nodes without regenerating the task DAG or repeating completed nodes.

Every ProductManager, Builder, and Tester Codex invocation records input, cached-input, output, reasoning-output, and total tokens together with its action, adapter, requested/effective model, requested/effective reasoning effort, and route source. Builder records also include the task difficulty used for routing. Step totals appear on the DAG node and invocation detail; the agent run stores the build total. Skill runtime Codex calls are excluded.

Model routing is backend policy, not ProductManager output. ProductManager actions may have separate user defaults. Builder task nodes use the existing validated `difficulty` value to select the user-configured `easy`, `medium`, or `hard` route, falling back to the Builder default. Tester task, final end-to-end, and update actions have independent routes. Changing a model never changes sandbox, permission, approval, retry, quota-reserve, or workspace boundaries.

## Builder Actions

### `builder_build_task`

Inputs:

- compact `permission_bounds` derived from approved `permissions.json`, including effective runtime permissions, approved build dependencies/research, and blocked capabilities;
- the current task node fields needed to build the node;
- direct-parent `interface_artifact.json` files only;
- backend API context for ids listed in the current task node's `backend_api_ids`;
- `workspace_paths` naming generated skill files the node may need. File contents are not embedded because Builder can read these paths inside its controlled workspace.

The Builder prompt must not include backend bookkeeping fields such as `generation_request_id`, `blueprint_path`, `permission_path`, `task_dag_path`, `task_path`, task `index`, or task `status`. It must not include full source snapshots, manifest requirement summaries already enforced by the backend, or interface-artifact field lists already defined by Builder instructions. It should not receive the entire task DAG for a normal node build; dependency contracts come from direct-parent interface artifacts. The backend may still use transitive lineage for deterministic created-versus-updated validation.

The full backend API context is loaded from the static file at `backend/app/static/backend_api_context.json`. ProductManager sees only the index file; Builder receives only the context entries selected by the current task node's `backend_api_ids`.

Behavior:

- build only the current task node;
- write only inside the controlled proposed skill folder;
- respect `file_write_claims` unless the backend grants an explicit serialized exception;
- treat the backend-seeded `manifest.json` as the package contract starting point instead of inventing a separate manifest shape;
- produce or update skill package files for the node;
- write temporary `interface_artifact.json` at the controlled skill-folder root;
- let the backend validate the sidecar and move it to `runtime/agent_runs/run_<id>/tasks/<task_id>/interface_artifact.json`; Builder never writes under `runtime/agent_runs`;
- call Codex from generated skill code only through the backend Skill Codex Call API when that API context is provided; never shell out to the Codex CLI.

Interface artifact shape:

```json
{
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

All five top-level fields are required and unknown top-level fields are rejected. The backend adds `task_id` from workflow context only when passing the artifact to another agent. Declared paths must be unique relative files inside the skill folder, must exist, and must be covered by the task's `file_write_claims`; `manifest.json` is the sole backend-owned exception. `created_paths` and `updated_paths` must not overlap. Files exposed by parent artifacts and `manifest.json` are updates; files introduced by the current task are creations. The backend performs these checks before replacing any run artifact. A missing or invalid sidecar fails the Builder node and remains in the skill folder for inspection.

### `builder_fix_task`

Inputs:

- `permissions.json`;
- all compact `builder_build_task` inputs for the node;
- the current node's last backend-validated interface artifact so created/updated declarations survive repair;
- one normalized failure object containing task id, error, and bounded stdout/stderr;
- current generated file paths, without embedding file contents.

Behavior:

- fix implementation files only;
- do not edit Tester-owned tests; the backend restores changed Python test sources and fails the Builder invocation if this boundary is crossed;
- preserve or narrow permissions unless the approved `permissions.json` explicitly allows the change;
- write a complete replacement `interface_artifact.json` for every repair attempt, preserving the task's created-versus-updated contract when interfaces did not change.

### `builder_fix_final_e2e`

Inputs:

- compact final blueprint contract;
- effective `permission_bounds`;
- final end-to-end expectations from `task_dag.json`;
- compact interface contracts without file-claim bookkeeping;
- one normalized final failure object;
- generated skill package paths without embedded contents.

Behavior:

- repair cross-node integration issues;
- keep changes inside the controlled skill folder;
- do not edit final end-to-end tests;
- do not expand permissions without a new deterministic permission review.

## Tester Actions

### `tester_test_task`

Runs only for nodes with `requires_tests = true`.

Inputs:

- the current task node;
- direct-parent interface artifacts;
- `workspace_paths` for Builder-created files needed to test the node. Source contents are not duplicated in the prompt.

The Tester prompt for a node must not include the entire task DAG, task artifact paths, task status, task index, or other backend-only bookkeeping. It should receive `test_file` so it writes only the node-specific test file.

Behavior:

- write tests that match the node's `test_expectations` and acceptance criteria;
- write node-specific test files such as `tests/test_<task_id>.py` so parallel testers do not edit the same file;
- run at most one focused pytest self-check and correct only test-owned harness mistakes, without weakening requirements or editing implementation;
- run tests through the safe validation path;
- write `tasks/<task_id>/test_result.json`;
- on failure, write `tasks/<task_id>/failure.log` and trigger `builder_fix_task`.

### `tester_final_e2e`

Runs after every task node is `done`.

Inputs:

- compact blueprint goal, expected behavior, schedule, and acceptance criteria;
- final end-to-end expectations from `task_dag.json`;
- compact interface contracts;
- safe package `workspace_paths`, excluding `.git`, `.agents`, caches, bytecode, and Codex bookkeeping files.

Behavior:

- write one final end-to-end test file, for example `tests/test_final_e2e.py`;
- validate the whole proposed skill against the blueprint and final DAG expectations;
- run the safe validation/test path;
- write `final_e2e_test_result.json`;
- on failure, trigger `builder_fix_final_e2e` and repeat until tests pass or the failure limit is reached.

## Failure Limits

Each task node has its own fix counter. After more than three failed test/fix attempts for one node, ProductManager writes a stuck user-facing summary and the run stops as `blocked`.

When a build run fails after a task has already produced a validated interface artifact, Retry Current Task reuses the persisted DAG, task contract, skill workspace, and failure count. It reruns only that task's repair/test loop and then continues with remaining tasks and final E2E; it does not regenerate ProductManager artifacts or repeat completed Builder work.

The final end-to-end loop has a separate failure counter. After more than three final failures, ProductManager writes a stuck user-facing summary and the run stops as `blocked`.

## Completion

For `task_dag`, after all task nodes and the final end-to-end test pass:

1. Backend deterministically finalizes any missing fields in the actual `manifest.json` from the approved blueprint and permission plan.
2. Backend validates the actual `manifest.json`, declared package files, and tests.
3. Backend creates runtime permission review from the actual manifest and dependencies.
4. ProductManager does not write a completion summary unless the workflow is blocked or needs user input.
5. Chat tells the user only that the project is finished and surfaces any runtime permission approval needed before install.
6. The skill remains proposed until the user explicitly installs or rejects it.

For `single_codex`, successful Codex completion and runtime permission review mark the agent run succeeded and leave the skill proposed. Independent backend package/test validation is intentionally deferred to the TODO described above.

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
- Builder must write a skill-local `interface_artifact.json` for every node; the backend validates and moves it into the node's run-artifact folder so child nodes have explicit contracts without granting Builder write access to `runtime`.
- When a newly persisted agent-run ID collides with an orphaned `runtime/agent_runs/run_<id>` directory, the backend preserves the stale directory under `runtime/agent_runs/orphaned/` and initializes a clean directory for the new run.
- Tester writes node-specific test files and one final end-to-end test file instead of sharing one test file across all build work.
- Backend seeds `manifest.json` from `blueprint.json` and `permissions.json`, and later fills missing manifest fields deterministically when possible.
