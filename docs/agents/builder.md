# BuilderAgent

BuilderAgent writes and repairs generated skill files inside controlled skill folders, one DAG task node at a time. Permission classifications come from `backend/app/static/default_permissions.json`, not Builder instruction prose.

## Modes

```text
build
repair
update
```

## Build Mode

Builder reads:

- compact backend-approved permission bounds with effective runtime permissions and a `blocked` field loaded from the canonical config;
- the current task's `task_prompt`, `write_paths`, and `acceptance_criteria` only;
- full function context only for catalog ids assigned by ProductManager on the current task node;
- interface artifacts from direct parent task nodes;
- safe workspace paths for existing generated files when applicable. File contents are read from the controlled workspace instead of duplicated in the prompt.

Builder should not be prompted with the node id, selected function ids, dependencies, difficulty, test policy, parallel-admission policy, test expectations, backend artifact paths, task indexes, task status, generation request ids, or the entire task DAG for ordinary node work. Selected function ids are resolved by the backend into detailed `function_context`. Builder also should not receive duplicated manifest requirements, interface-artifact schemas, full source snapshots, transitive ancestor artifacts, or function context for other nodes.

Builder implements the current task node only. It must not jump ahead to child nodes unless the current node explicitly defines shared setup as part of its acceptance criteria.

Builder reads only the named workspace paths plus the backend-seeded manifest. It does not recursively inventory internal metadata, initialize version control, run tests, or repeatedly reread unchanged files; backend validation and Tester own those actions.

Builder may use only entries in `function_context` and must follow each entry's literal id, input/output schemas, trusted helper, rules, and test guidance. User functions use `function_runtime_capabilities.call_function` or the server-side web-app equivalent. Backend Codex calls use the catalog-documented backend route/helper and never invoke the Codex CLI. Integration functions use their trusted literal operation helper; Builder must not contact GitHub directly, construct authentication, call an internal relay path, expose integration use to browser code, or import the deterministic test adapter from implementation. Multi-item Codex work uses one bounded batched request with per-item result mapping instead of sequential per-item calls.

For new build workflows, the backend creates a skeleton `manifest.json` and the skill's `tests/` directory before the first writable agent step. Builder should preserve the manifest shape and the backend-owned test directory and complete only the current node's package details.

After each successful task build, Builder writes `interface_artifact.json` at the controlled skill-folder root for child nodes. The artifact must name created/updated paths, schemas, entrypoints, functions, data contracts, and known limitations relevant to downstream work. The backend validates the sidecar before moving it to `runtime/agent_runs/run_<id>/tasks/<task_id>/interface_artifact.json`; Builder never writes under `runtime`.

For `runtime = function`, Builder preserves bounded JSON stdin/stdout and does not add a user-facing interface. For `runtime = service`, Builder uses the same bounded protocol, preserves the required schedule, and does not expose a manual, function, MCP, HTTP, or user-interface entrypoint. For `runtime = web_app`, Builder owns package-local interface files while leaving Eidolon frontend source unchanged.

For `runtime = web_app`, Builder exposes the manifest-declared importable ASGI application and may create package-owned HTML/CSS/JavaScript. Read-only assets use module-relative paths. Persistent state resolves from `PERSONAL_AGENT_SKILL_CACHE_DIR`, with `./cache` only as a development fallback; writable cache must never be placed beneath `__file__` because the package is read-only at runtime. Browser code uses same-origin application routes only. Builder never creates Eidolon React source, custom Dockerfiles, startup commands, or process-management code.

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
- Add secrets, broad filesystem access, unrestricted network access, browser automation, direct email/calendar/finance actions outside declared trusted integrations, purchases, public posting, trading, file deletion, or arbitrary command execution.
