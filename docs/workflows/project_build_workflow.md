# Project Build Workflow

New skill builds share one general starting sequence and then dispatch to a backend-managed build workflow. A one-time intent placeholder is followed by the resumable `pm_plan_build` session, which combines clarification, rejection, blueprinting, permission planning, and workflow selection before deterministic build-time approval. The supported build workflows are `single_codex` and `task_dag`.

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
The actual Codex input also includes the corresponding instructions.
ProductManager does not write these files directly; the backend validates its JSON response and writes the artifacts.

1. `pm_refine_intent`
   - Input: the initial Project-mode user message.
   - Output: `intent_prompt.json`.
   - Current placeholder behavior: copy the initial user message exactly into `refined_prompt` with `schema_version = 1`.
   - This step runs once when the generation request is created. It performs no memory lookup or extraction and does not compose a prompt, route a model, or invoke Codex. Clarification replies do not rerun it.
   - Only `schema_version` and `refined_prompt` continue to the planning session.
   - It must not create a blueprint, permissions, task graph, or generated skill files.

2. `pm_plan_build`
   - First-turn inputs: `intent_prompt.json`, the available function-catalog index, and complete config-derived `permission_policy` containing `default_allowed`, `requires_approval`, and `blocked`.
   - Clarification-turn input: only the latest user answer; the same persistent Codex App Server thread is resumed.
   - Response: `decision`, `user_prompt`, `build_workflow`, `blueprint`, and `permission_plan` on every turn. The backend writes the validated decision and prompt to `decision.json` after every turn.
   - Codex App Server receives the complete structured blueprint object in its output schema. Only free-form nested JSON values (`input_schema`, `output_schema`, and scheduled input) are transported as JSON strings, decoded to objects, and then checked against the canonical backend schema.
   - Decisions are `ask_user_for_input`, `stop_inplausible`, or `proceed_to_approval`. Clarification and rejection contain no planning fields; approval handoff requires a complete workflow, blueprint, and permission plan.
   - No blueprint, permission, DAG, or generated skill artifacts are created until `proceed_to_approval`.
   - Purpose: choose `single_codex` or `task_dag`; provide one safe skill `name` and concise `description`; select the `function`, `service`, or `web_app` execution protocol; define complete function/service input/output schemas; describe expected user behavior and service-only schedule intent; select catalog functions; and draft both build-time needs and expected runtime permissions/dependencies.
   - ProductManager receives only available catalog ids, titles, descriptions, categories, and risks, never detailed schemas, endpoints, credential management, or secret-store details.
   - `build_workflow` is backend routing state stored on the agent run. It must not appear inside `blueprint` or in `blueprint.json` because downstream DAG, Builder, and Tester inputs do not need it.
   - Must not enumerate generated package files. Required Builder-owned paths are defined later by each task node's `write_paths` in `task_dag.json`.
   - Must not include task nodes, dependencies between tasks, or test files.
   - Must not approve permissions.
   - Must omit default-allowed values, return only the exact `requires_approval` shape, and never request a blocked capability. The structured output schema and backend sanitizer derive this contract from the config rather than instruction prose.
   - ProductManager owns the single `name` in the blueprint. The backend validates it before creating the controlled database record and package folder, then derives any presentation label needed by the UI.
   - Integration providers and operations are derived from selected function ids. Provider-specific resource authorization is validated from the generated manifest during runtime permission review, not carried as free-form ProductManager scope data.

The Codex Settings page can persist a backend-owned workflow override. `Automatic` keeps the ProductManager choice. `Simple` forces `single_codex`, and `Task DAG` forces `task_dag` for every new Project build, regardless of the top-level value returned by ProductManager. The agent run stores the effective workflow and the ProductManager step records whether selection came from ProductManager or the settings override.

3. Backend deterministic permission review
   - Inputs: `blueprint.json`, `permissions.json`.
   - Output: if approval required, build-time approval request in chat, otherwise proceed automatically.
   - Purpose: Manage permissions safely.
   - Approval lets the backend provision the listed Python dependencies and lets Codex generate proposed files. It does not install or run the skill, schedule it, or approve runtime permissions.

4. Backend dependency provisioning
   - Inputs: the approved dependency allowlist and effective `permissions.json`.
   - Output: a clean controlled skill workspace with runtime dependencies in `.deps` and any missing build-only dependencies in `.build-deps`.
   - Purpose: install and verify approved packages before any post-approval ProductManager, Builder, or Tester invocation starts.
   - The backend interpreter, Codex subprocess environment, and authoritative test runner share these dependency paths. Platform test tooling such as `pytest` is verified as backend infrastructure and is provisioned into `.build-deps` only when missing.
   - Installation failure, unavailable packages, approval mismatch, or distribution verification failure stops the workflow before Codex starts. Repeated validation verifies and reuses the provisioned environment; it does not run `pip` again.

5. Backend workflow dispatch
   - Inputs: the agent run's backend-only `build_workflow`, approved `blueprint.json`, and effective `permissions.json`.
   - Output: execution by the registered `single_codex` or `task_dag` workflow module.
   - The registry rejects unknown workflow names. Workflow selection and execution are not hard-coded into the common pre-approval sequence.

6. `pm_write_task_dag` (`task_dag` only)
   - Inputs: `blueprint.json`, compact backend-approved `permission_bounds`, and the concise index of functions selected in the blueprint.
   - Output: `task_dag.json`.
   - Purpose: split the project into explicit task nodes with an `id`, direct `task_prompt`, dependencies, difficulty, test requirement, parallel-safety flag, required `write_paths`, acceptance criteria, test expectations, and selected function ids.
   - Must not include separate "test-only" task nodes. Tester actions are attached to the build nodes that require tests.

## Single-Codex Workflow

The `single_codex` workflow package contains `workflow.py`, `prompts.py`, and `instructions/run.md`. Before the writable invocation, the backend creates the controlled proposed-skill folder, provisions and verifies approved dependencies, seeds `manifest.json`, and creates its `tests/` directory. The prompt contains the approved blueprint, config-derived effective `permission_bounds`, fixed workflow instructions, and full context for every catalog function selected in the blueprint. Within that one invocation Codex plans internally, creates the complete skill package, writes test files inside the existing backend-created `tests/` directory, runs a focused test command, and fixes failures before returning. It must not create or replace the test directory.

After the writable invocation returns, the backend runs the shared deterministic final validator: static capability scan, actual manifest/package validation, and authoritative execution of the generated tests. This validator does not invoke ProductManager, Builder, Tester, or another Codex agent. Any invocation or validation error permanently stops that single-Codex run; resume, task retry, and step retry cannot invoke Builder again. The user may start a separate new Project build, whose proposed workspace is atomically replaced without descending into sandbox-owned cache directories. Runtime permission review is created only after final validation passes.

Cancellation records a terminal cancelled state and terminates the Codex subprocess owned by that agent run. Cancellation and successful finalization share one backend transition boundary: if cancellation wins, no runtime permission request or successful finalization may be created afterward; if finalization wins, a later cancellation is a no-op because the run is already terminal. A cancelled proposed package is marked failed and cannot be installed without explicit deterministic recovery.

The workflow can pause before its single invocation when Codex allowance is below the configured reserve. A pre-invocation allowance pause may resume, but an invocation or validation error cannot resume or retry.

## Shared Final Validation

Both project-build workflows use one deterministic final package validator after implementation and workflow-specific test authoring complete:

1. The backend reads and package-validates the actual generated `manifest.json` and confirms its runtime matches the approved blueprint runtime.
2. The manifest dependency list must exactly match the backend-provisioned runtime dependency contract, and every declared distribution must still be present at its approved version.
3. It scans implementation `.py` files plus literal absolute URLs in HTML/CSS/JavaScript assets while excluding tests, dependency folders, caches, and metadata.
4. Undeclared network use, browser-side external URLs, unapproved literal server domains, writes outside approved runtime paths, sensitive environment reads, process execution, browser automation, file deletion, or unscannable source fail validation. When the scan passes, the backend runs the tests it finds in the backend-created `tests/` directory.
5. The backend persists `capability_scan.json` plus `final_e2e_test_result.json` without calling an agent.
6. A `single_codex` failure blocks the run. A `task_dag` failure may enter the DAG-owned bounded Builder repair loop; every repaired result is scanned and validated again.
7. Runtime permission review is not created until the scan, manifest/package validation, and tests pass.

In `task_dag`, Tester writes `tests/test_final_e2e.py` before this validator runs, using the approved blueprint expected behavior and validated node interface artifacts. There is no separate final-expectations field in `task_dag.json`. In `single_codex`, the one writable invocation is responsible for writing its tests from the approved blueprint.

The capability scan is deliberately small and evidence-based. It blocks selected recognized calls, provably unsafe literal paths or domains, unscannable source, and literal browser-asset URLs. Imports without recognized calls, dynamic paths, and runtime-constructed domains do not block when the scanner cannot prove a violation. The runtime sandbox remains authoritative; the scan cannot prove the absence of dynamic, transitive, encoded, dependency-internal, Python-embedded browser, or runtime-constructed behavior.

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
        "task_prompt": "specific work BuilderAgent should complete for this task node",
        "depends_on": [],
        "difficulty": "easy|medium|hard",
        "requires_tests": true,
        "parallel_safe": true,
        "write_paths": ["skill.py"],
        "acceptance_criteria": ["string"],
        "test_expectations": ["string"],
        "function_ids": []
      }
    ]
  }
}
```

`requires_tests` and `parallel_safe` are JSON booleans. Root nodes are derived from an empty `depends_on` list. `manifest.json` is excluded from `write_paths`; the backend owns it and grants Builder a serialized exception when a task needs to update it. A task id is the sole stable node label; the UI derives its display text from that id. `task_prompt` is the direct implementation assignment sent to Builder and Tester.

Backend validation must reject the graph when:

- the graph is cyclic;
- a dependency references a missing node;
- a node id is not a safe path segment;
- a node omits its task prompt, acceptance criteria, or write paths;
- a node references a function not selected in the approved blueprint;
- a selected blueprint function is not assigned to any node;
- the skill has no tested node;
- two simultaneously ready nodes have overlapping `write_paths` without an explicit dependency ordering them.

`write_paths` is both the node's required output set and its allowed package-write boundary. The backend requires every listed path in the Builder's interface artifact, rejects other declared paths except `manifest.json`, and prevents unordered nodes from owning the same path.

Task `write_paths` are Builder-owned skill package paths only. They must not include Tester-owned files such as `tests/test_skill.py` or `tests/test_<task_id>.py`; the backend sanitizes those paths out of ProductManager DAG output before validation.

Function and service tasks normally claim a Python file entrypoint such as `skill.py`; services preserve scheduler-only exposure. Web-application tasks claim an importable ASGI module such as `app.py` and may claim skill-owned HTML/CSS/JavaScript assets. No runtime may claim Eidolon frontend files, custom Dockerfiles, or startup commands.

The blueprint does not define package file paths. ProductManager assigns Builder-owned paths directly to task nodes in `task_dag.json`; the backend does not invent omitted task outputs. `README.md` and `SKILL.md` are not universal package requirements; `manifest.json` remains backend-owned and is always validated, including any entrypoint or instructions file it declares.

### Task Node Execution

After approval and DAG validation, the backend builds the DAG data structure and repeatedly executes ready nodes:

1. Before the first Builder step, the backend derives a skeleton `manifest.json` in the proposed skill folder from `blueprint.json` and `permissions.json` and creates the skill's `tests/` directory.
2. A node is ready when all `depends_on` nodes are `done`.
3. The backend groups non-overlapping `parallel_safe` ready nodes into one admitted batch. The current shared-workspace executor processes that batch serially; true concurrent execution remains deferred.
4. Each active node creates one Builder step.
5. After each Builder step, the backend deterministically fills missing approved package declarations when possible and removes legacy backend-owned lifecycle/risk/provenance keys. It does not infer permissions from source.
6. If `requires_tests` is true, the Builder step is followed immediately by a Tester step for the same node.
7. A node is `done` only after Builder succeeds, the backend validates and moves its skill-local `interface_artifact.json` into the task's run-artifact folder, and required tests pass.
8. A node with `requires_tests = false` is still subject to deterministic package validation before it can be marked `done`.

Node statuses:

```text
pending, ready, building, testing, fixing, done, failed, blocked
```

The backend groups simultaneously ready `parallel_safe` nodes into execution batches. The current shared skill workspace processes nodes within an admitted batch deterministically, while the batch boundary preserves the same safety contract for a parallel executor: all nodes already admitted to the batch finish before a quota pause, and no node from the next ready batch starts.

Before admitting another ready batch and after an admitted batch finishes, the backend refreshes the Codex account allowance. If either the 5-hour or weekly window has less than 5% remaining, the agent run becomes `paused`. Exactly 5% remains runnable. The workflow page shows the reset time and a Resume control. Resume rechecks both windows and continues from persisted `done` nodes without regenerating the task DAG or repeating completed nodes.

Every ProductManager, Builder, and Tester Codex invocation records the exact composed prompt and exact final response in addition to input, cached-input, output, reasoning-output, and total tokens, action, adapter, requested/effective model, requested/effective reasoning effort, and route source. Builder records also include the task difficulty used for routing. Step totals appear on the DAG node and invocation detail; the agent run stores the build total. Skill runtime Codex calls are excluded. Backend-only permission, dependency, validation, finalization, and bounded-stop work is labeled separately and has no agent transcript.

Model routing is backend policy, not ProductManager output. ProductManager actions may have separate user defaults. Builder task nodes use the existing validated `difficulty` value to select the user-configured `easy`, `medium`, or `hard` route, falling back to the Builder default. Tester task, final end-to-end, and update actions have independent routes. Changing a model never changes sandbox, permission, approval, retry, quota-reserve, or workspace boundaries.

## Builder Actions

### `builder_build_task`

Inputs:

- compact `permission_bounds` derived from approved `permissions.json`, including effective runtime permissions, approved build dependencies/research, and the config-derived `blocked` field;
- a Builder-only task projection containing `task_prompt`, `write_paths`, and `acceptance_criteria`;
- direct-parent `interface_artifact.json` files only;
- full function context only for ids listed in the current task node's `function_ids`;
- `workspace_paths` naming generated skill files the node may need. File contents are not embedded because Builder can read these paths inside its controlled workspace.

The Builder prompt does not include the node id, function ids, dependency ids, difficulty, test policy, parallel-admission policy, or test expectations. Those remain backend orchestration inputs. It must also omit bookkeeping fields such as `generation_request_id`, `blueprint_path`, `permission_path`, `task_dag_path`, `task_path`, task `index`, and task `status`. It does not include full source snapshots, manifest requirement summaries already enforced by the backend, or interface-artifact field lists already defined by Builder instructions. It does not receive the entire task DAG for a normal node build; dependency contracts come from direct-parent interface artifacts. The backend may still use transitive lineage for deterministic created-versus-updated validation.

The backend resolves full context from the persistent unified catalog. ProductManager sees the concise available-only index; Builder receives schemas, examples, constraints, invocation guidance, and test guidance only for the current node's selected ids. Integration function code must use `integration_runtime_capabilities.call`; web-application server code must use `web_runtime_capabilities.call_integration`.

Behavior:

- build only the current task node;
- write only inside the controlled proposed skill folder;
- create or update every `write_paths` entry and modify no other package path unless the backend grants the `manifest.json` exception;
- treat the backend-seeded `manifest.json` as the package contract starting point instead of inventing a separate manifest shape;
- preserve the backend-derived manifest requirements for blueprint-selected functions and use only their catalog-documented trusted helpers;
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

All five top-level fields are required and unknown top-level fields are rejected. The backend adds `task_id` from workflow context only when passing the artifact to another agent. Declared paths must be unique relative files inside the skill folder, must exist, and must match the task's required `write_paths`; `manifest.json` is the sole backend-owned exception. `created_paths` and `updated_paths` must not overlap. Files exposed by parent artifacts and `manifest.json` are updates; files introduced by the current task are creations. `contracts_for_children` describes the actual implemented contract rather than repeating a ProductManager prediction. The backend performs these checks before replacing any run artifact. A missing or invalid sidecar fails the Builder node and remains in the skill folder for inspection.

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
- approved blueprint expected behavior;
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

- a Tester-only task projection containing `task_prompt`, `acceptance_criteria`, and `test_expectations`;
- direct-parent interface artifacts;
- `workspace_paths` for Builder-created files needed to test the node. Source contents are not duplicated in the prompt.
- selected integration operation context and the deterministic fake adapter marker when the node uses GitHub.

The Tester prompt for a node does not include the node id, function ids, dependency ids, difficulty, Builder write paths, test-admission boolean, or parallel-admission policy. It must not include the entire task DAG, task artifact paths, task status, task index, or other backend-only bookkeeping. It receives the backend-derived `test_file` so it writes only the node-specific test file.

The backend creates the skill's `tests/` directory before any Builder or Tester action. Tester writes only the named test file inside that existing directory and must not create, replace, rename, or delete the directory or create a second test folder.

Behavior:

- write tests that match the node's `test_expectations` and acceptance criteria;
- write node-specific test files such as `tests/test_<task_id>.py` so parallel testers do not edit the same file;
- run at most one focused pytest self-check and correct only test-owned harness mistakes, without weakening requirements or editing implementation;
- run tests through the safe validation path;
- write `tasks/<task_id>/test_result.json`;
- on failure, write `tasks/<task_id>/failure.log` and trigger `builder_fix_task`.
- never use a live GitHub credential or request; verify helper use, normalized output, and failure behavior with the deterministic fake adapter.

### `tester_final_e2e`

Runs after every task node is `done`.

Inputs:

- compact blueprint description, expected behavior, and schedule;
- approved blueprint expected behavior;
- compact interface contracts;
- safe package `workspace_paths`, excluding `.git`, `.agents`, caches, bytecode, and Codex bookkeeping files.
- detailed integration context only for operations selected across the approved build, plus the deterministic fake adapter marker.

Behavior:

- write one final end-to-end test file, for example `tests/test_final_e2e.py`;
- write coverage for the whole proposed skill against the blueprint expected behavior;
- run at most its focused self-check; the backend separately owns authoritative package validation and test execution;
- leave `final_e2e_test_result.json` to the backend validator;
- never approve the result or trigger repair itself. The DAG workflow evaluates backend validation and may invoke `builder_fix_final_e2e`.

## Failure Limits

Each task node has its own fix counter. After more than three failed test/fix attempts for one node, deterministic backend logic writes the bounded-stop summary and the run stops as `blocked`.

For `task_dag` only, when a build run fails after a task has already produced a validated interface artifact, Retry Current Task reuses the persisted DAG, task contract, skill workspace, and failure count. It reruns only that task's repair/test loop and then continues with remaining tasks and final E2E; it does not regenerate ProductManager artifacts or repeat completed Builder work. Single-Codex errors expose no run-level or step-level retry action.

The final end-to-end loop has a separate failure counter. After more than three backend validation failures, deterministic backend logic stops the run as `blocked`; final validation does not invoke ProductManager for a summary.

## Completion

For `task_dag`, after all task nodes and the final end-to-end test pass:

1. Backend deterministically finalizes missing approved package fields in the actual `manifest.json` from the blueprint and permission plan; backend lifecycle state and derived risk stay outside the manifest.
2. Backend validates the actual manifest runtime against the blueprint, validates declared package files, scans capability mismatches, and runs tests.
3. Backend creates runtime permission review from the actual manifest and dependencies.
4. Deterministic backend logic writes the completion or bounded-stop summary; ProductManager is invoked only for its explicit planning/review actions or user-input decisions.
5. Chat tells the user only that the project is finished and surfaces any runtime permission approval needed before install.
6. The skill remains proposed until the user explicitly installs or rejects it.

For `single_codex`, successful Codex completion must be followed by the shared static capability scan and backend manifest/package/test validation. No independent Tester or repair agent is invoked. Only then does runtime permission review mark the agent run succeeded and leave the skill proposed.

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
- `build_next_milestone` is replaced by `proceed_to_approval`; the combined planning session returns the validated workflow, blueprint, and permission plan together.
- Per-node `requires_tests` controls whether Tester runs immediately after Builder.
- Per-node `write_paths` provides required-output validation and non-overlapping ownership for safe parallel Builder/Tester execution.
- Builder must write a skill-local `interface_artifact.json` for every node; the backend validates and moves it into the node's run-artifact folder so child nodes have explicit contracts without granting Builder write access to `runtime`.
- When a newly persisted agent-run ID collides with an orphaned `runtime/agent_runs/run_<id>` directory, the backend preserves the stale directory under `runtime/agent_runs/orphaned/` and initializes a clean directory for the new run.
- Tester writes node-specific test files and one final end-to-end test file instead of sharing one test file across all build work.
- Backend seeds `manifest.json` from `blueprint.json` and `permissions.json`, and later fills missing manifest fields deterministically when possible.
