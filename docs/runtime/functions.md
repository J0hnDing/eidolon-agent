# Function Registry and Invocation

`runtime = function` is the machine-facing, one-shot JSON protocol. Installed functions do not host HTTP services. The backend records backend-core, installed user, and integration functions in one persistent catalog at `runtime/function_catalog.json`.

## Registry Contract

`GET /functions/catalog` returns all catalog entries with category, description, input/output JSON Schemas, invocation guidance, derived risk, availability state, and availability reasons. This is the source for the ProductManager catalog and Functions UI. Only available entries are included in ProductManager prompts.

`GET /functions` remains the runtime-facing installed user-function discovery route. Results never expose package paths, entrypoint commands, credentials, container identities, or unrelated lifecycle state.

An installed function is available for registry invocation only when it is enabled, has a current active function version, has valid object-shaped input/output schemas, has approved and supported runtime permissions, and its active manifest matches the backend identity. Disabled or otherwise unavailable functions remain discoverable with explicit reasons.

Discovery is not authorization. ProductManager selects catalog ids in `blueprint.functions`; the backend derives installed user-function names in the caller manifest. A caller manifest contains:

```json
{
  "function_requirements": ["normalize_text"]
}
```

For Task DAG builds, ProductManager assigns approved catalog ids to nodes through `function_ids`; the backend gives each Builder full context only for that node. Single-Codex receives full context for every selected function. Function code uses the trusted `function_runtime_capabilities.call_function` helper. Web-application server code uses `web_runtime_capabilities.call_function`. Browser code never receives either capability token.

GitHub and Atlas integration calls use the parallel stable helper `integration_runtime_capabilities.call`. The operation must be literal, declared by the active manifest, and approved for the caller's current provider fingerprint; GitHub repository operations must also remain within exact scope. The helper carries no credential. After invocation checks, the backend retrieves GitHub's token when needed or calls Atlas's unlocked native API without authentication, then returns the bounded normalized result. See [GitHub integration](../integrations/github.md) and [Atlas integration](../integrations/atlas.md).

Function skills call the selected `backend.codex.call` capability through `function_runtime_capabilities.call_codex`. The helper sends the ephemeral function-run bearer token through the private relay; the backend resolves the caller and active version from that token rather than trusting a submitted skill id. Codex call/response and optional internet access remain governed by the active manifest and approved runtime permissions. The legacy `POST /skills/{skill_id}/codex` route remains available to trusted local callers for compatibility but is not the sandbox transport.

For concise extension blueprints covering user, integration, and backend-core functions, see [Extending the function catalog](function_extension_guide.md).

## Authorization

The backend evaluates the direct caller-to-target relationship on every call:

- the caller identity comes from an ephemeral capability tied to the currently running caller version or from a current web-application instance capability;
- the caller and target must remain installed, enabled, version-current, and normally runnable;
- the active caller manifest must explicitly declare the target;
- input JSON must satisfy the target input schema;
- low-risk targets need no extra caller-specific approval;
- medium- and high-risk targets need an approved caller-target relationship;
- unsupported or blocked target permissions remain unavailable regardless of relationship approval.

Function-access approval is specific to one caller skill and one target function. Its fingerprint covers target risk, permissions, dependencies, and input/output schemas. Code-only target version changes retain approval; a changed fingerprint makes the relationship stale and requires review. Approval does not authorize other callers or inherit the target's permissions.

Direct user runs, backend actions, and approved schedules keep their existing authorization boundaries. They do not create synthetic caller skills. All paths converge on the registry service for availability checks, operation locking, bounded execution, and audit attribution.

## Runtime and Audit

The target still runs through the disposable function runner and creates its normal `skill_runs` record. Runs record target version, invocation source, caller skill/version when applicable, schedule id or web-app instance when applicable, and a bounded initiating-action label. Runtime Codex usage remains attached to that target run and separate from Project build totals.

No-internet Docker callers receive backend Function and integration capability access through a transient allowlisted relay on an internal Docker network. The caller cannot use those exact paths for general backend access or internet egress. Functions with approved runtime network domains continue to use the existing bridge behavior, but direct GitHub and Atlas access is still prohibited; domain-level egress filtering remains separately disclosed.

Input mismatch blocks before entrypoint execution and is audited as a blocked target run. Output mismatch changes the completed target run to failed while preserving its output and process diagnostics.

Nested function calls are intentionally blocked in this milestone. The deferred design work is tracked in `TODO-013`.

## Compatibility

Manual bounded runs, approved schedules, active-version switching, runtime permissions, and existing run history remain supported. Older function packages without JSON schemas remain directly runnable and schedulable, but cannot be used as registry targets until updated. Packages remain exactly one runtime protocol; a web application calling a function does not become a hybrid package.
