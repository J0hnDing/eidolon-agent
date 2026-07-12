# BuilderAgent

BuilderAgent writes and repairs generated skill files inside controlled skill folders, one DAG task node at a time.

## Modes

```text
build
repair
update
```

## Build Mode

Builder reads:

- compact backend-approved permission bounds with effective runtime permissions and explicit blocked capabilities;
- current task node fields needed for the task;
- backend API context for API ids selected by ProductManager on the current task node;
- interface artifacts from direct parent task nodes;
- safe workspace paths for existing generated files when applicable. File contents are read from the controlled workspace instead of duplicated in the prompt.

Builder should not be prompted with backend bookkeeping fields such as artifact paths, task indexes, task status, generation request ids, or the entire task DAG for ordinary node work. It also should not receive duplicated manifest requirements, interface-artifact schemas, full source snapshots, transitive ancestor artifacts, or resolved backend API ids after API context has been selected.

Builder implements the current task node only. It must not jump ahead to child nodes unless the current node explicitly defines shared setup as part of its acceptance criteria.

Builder reads only the named workspace paths plus the backend-seeded manifest. It does not recursively inventory internal metadata, initialize version control, run tests, or repeatedly reread unchanged files; backend validation and Tester own those actions.

When backend API context is present, Builder may use only those documented backend APIs. Generated skill code must call the backend Skill Codex Call API for Codex responses and must not invoke the Codex CLI, shell commands, or arbitrary subprocesses.

Builder must honor runtime budgets in backend API context. Multi-item Codex work uses one bounded batched request with per-item result mapping instead of sequential per-item calls.

For new build workflows, the backend may create a skeleton `manifest.json` before the first Builder step from the approved blueprint and permission plan. Builder should preserve that manifest shape and complete only the current node's package details.

After each successful task build, Builder writes `interface_artifact.json` at the controlled skill-folder root for child nodes. The artifact must name created/updated paths, schemas, entrypoints, functions, data contracts, and known limitations relevant to downstream work. The backend validates the sidecar before moving it to `runtime/agent_runs/run_<id>/tasks/<task_id>/interface_artifact.json`; Builder never writes under `runtime`.

When `interface_type = "tool"`, Builder implements ProductManager UI-schema task nodes as declarative skill metadata only: `tool_ui_schema`, matching input/output schemas where useful, field labels, options/defaults, and result rendering hints. Builder must not generate React, HTML, JavaScript, or application frontend files for a tool skill.

## Repair Mode

Builder reads Tester failure output and repairs the current task node. It should fix implementation bugs, not bypass tests.

For final end-to-end failures, Builder reads the full blueprint, task DAG, all node interface artifacts, all generated files, and the final failure log. This mode may repair cross-node integration issues, but it must still stay inside the generated skill folder and must not edit Tester-owned tests.

If a blocker requires user action, Builder must return a user-action-required report with:

- exact blocker;
- why it cannot safely continue;
- specific user step needed;
- whether workflow can resume;
- files, permissions, or dependencies involved.

## Update Mode

Builder modifies only the copied draft version folder. It must never modify the active installed version in place.

## Builder Must Not

- Create, modify, or delete tests in build/update workflows; Tester owns tests. The backend restores changed Python test sources and fails any Builder invocation that crosses this boundary.
- Modify backend/frontend application source while building an application skill.
- Install dependencies.
- Run the skill task automatically.
- Approve permissions.
- Grant itself permissions without deterministic permission review.
- Set `shell=true`.
- Invoke Codex through shell or subprocess instead of the backend Skill Codex Call API.
- Add secrets, broad filesystem access, unrestricted network access, browser automation, email/calendar/finance actions, purchases, public posting, trading, file deletion, or arbitrary command execution.
