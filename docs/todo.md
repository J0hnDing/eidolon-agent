# TODO: After finish, remove the corresponding task.

- Implement skill runtime token tracking.

## Local non-agentic model adapter. Where backend would read the path, parth context, feed into local/non-agentic model, read/parse output, write into corresponding files.  

## Per-Task Model Routing

Allow the user to select different Codex models for normal chat and for individual ProductManager, Builder, and Tester actions in a DAG workflow.

Suggested design:

1. Define independent defaults for Chat mode and each workflow role/action.
2. Allow a validated task-node model override without letting ProductManager invent arbitrary or unavailable model ids.
3. Decide whether model selection belongs in user settings, backend policy, the DAG schema, or a combination with explicit precedence.
4. Validate model availability before starting a workflow step and provide a clear fallback or blocking error.
5. Persist the requested and effective model on every Codex invocation for audit and token reporting.
6. Preserve sandbox, permission, quota-reserve, retry, and approval behavior regardless of model choice.
7. Expose chat model selection independently from project-build model routing.

## Codex CLI Version Compatibility

Prevent a stale `codex` executable on `PATH` from silently taking precedence over a newer Codex Desktop bundled CLI.

Suggested design:

1. Resolve and display the effective CLI executable and version used by the backend.
2. Preflight model and CLI compatibility before starting chat or a workflow.
3. Support an explicit `PERSONAL_AGENT_CODEX_COMMAND` override and a deterministic Codex Desktop bundled-CLI discovery fallback.
4. Fail with a concise actionable compatibility error when the selected model requires a newer CLI.
5. Avoid retrying a known-incompatible executable for every ProductManager, Builder, or Tester action.

## Real Parallel DAG Execution

The current scheduler is parallel-aware but executes an admitted ready-node batch serially in the shared skill workspace. Implement actual concurrent Builder and Tester execution without allowing concurrent mutation of that shared workspace.

Suggested design:

1. Compute the complete ready-node batch from satisfied dependencies.
2. Include only nodes marked `parallel_safe` whose declared `file_write_claims` do not overlap.
3. Create an isolated workspace for every node, using a copied skill workspace or another isolation mechanism appropriate for generated skill packages.
4. Start the isolated Builder invocations concurrently.
5. Run each node's Tester invocation and node-local tests in that node's isolated workspace.
6. Compare actual changed files with `file_write_claims`; reject undeclared writes, unsafe paths, and changes to backend-owned artifacts.
7. Detect conflicts using actual changes, not only ProductManager declarations.
8. Merge successful node outputs into the integration skill workspace in a stable, deterministic order.
9. Revalidate interface artifacts, the manifest, permissions, and the integrated package after merging.
10. Run integrated tests after every merged batch and retain the existing final end-to-end test.
11. Persist per-node status, logs, failures, and token usage without sharing a SQLAlchemy session or mutable Codex usage accumulator across worker threads.
12. Admit the whole ready batch using one quota decision, allow every admitted node to finish, and check the 5-hour and weekly 5% reserves before admitting the next batch.
13. On pause, failure, cancellation, or backend restart, clean up isolated workspaces safely while preserving diagnostic artifacts needed for retry.

Acceptance criteria:

- Independent ready nodes produce measurable wall-clock concurrency.
- Concurrent nodes cannot observe or overwrite another node's unmerged changes.
- Undeclared writes and merge conflicts block the affected batch with a clear user-facing reason.
- A failed node does not merge partial output from any isolated workspace.
- Retrying or resuming does not repeat already merged nodes.
- Existing per-skill operation locks, permission checks, token accounting, and quota-pause behavior remain enforced.
