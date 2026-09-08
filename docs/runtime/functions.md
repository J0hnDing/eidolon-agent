# Function Registry and Invocation

`runtime = function` is the machine-facing, one-shot JSON protocol. Installed functions do not host HTTP services. The backend records backend-core, installed user, and integration functions in one persistent catalog at `runtime/function_catalog.json`.

## Execution Kernel

All catalog callable invocations follow one backend route:

```text
authenticated adapter -> immutable InvocationContext -> InvocationExecutor -> category handler -> domain service/provider/runtime -> InvocationOutcome
```

`InvocationContextFactory` consumes agent credentials, bounded-run capability tokens, or scoped web-application instance capabilities and emits identifier-only attribution. Contexts distinguish the current principal (`user`, `agent`, `skill`, `web_app`, or trusted named `system`) from the originating transport. They contain no bearer token, agent credential, provider credential, or ORM object, and callers cannot supply context fields through invocation JSON.

Targets are explicitly category-qualified as `user`, `integration`, `backend_core`, or `agent_private`; a target string alone never selects a handler. `InvocationExecutor` rechecks live agent session and function policy, then delegates to the registered category handler. User functions continue through `FunctionRegistryService`. Integrations resolve the checked-in `IntegrationOperationRegistry`, authorize current state through `IntegrationCapabilityPolicy`, create a secret-free `AuthorizedIntegrationInvocation`, and only then dispatch through `IntegrationRuntime` to the registered provider adapter. `IntegrationService` remains only a connection/settings compatibility façade and is not an alternate catalog dispatcher. The checked-in backend-core handlers currently cover `act.document.download` and `backend.codex.call`; the private Assistant handler covers `plan_approval_request`.

## Registry Contract

`GET /functions/catalog` reads the persisted projection and returns all catalog entries with category, description, input/output JSON Schemas, invocation guidance, derived risk, availability state, availability reasons, and an `is_running` projection for active user-function runs and function audit records. It does not reconcile installed skill files, probe providers, or rewrite the catalog. Startup reconciles installed skills once and rebuilds the projection; skill lifecycle, runtime approval, active-version, and integration mutations rebuild it when relevant state changes. The transient running projection is added only to the UI response; only available entries and their stable contracts are included in ProductManager prompts.

The platform-owned `backend.notion.todo.cleanup_done` service is intentionally absent from the function catalog. `SchedulerService` registers and dispatches it through the endpoint-only platform-service boundary.

`GET /functions` remains the runtime-facing installed user-function discovery route. Results never expose package paths, entrypoint commands, credentials, container identities, or unrelated lifecycle state.

An installed function is available for registry invocation only when it is enabled, has a current active function version, has valid object-shaped input/output schemas, has approved and supported runtime permissions, and its active manifest matches the backend identity. Disabled or otherwise unavailable functions remain discoverable with explicit reasons.

Discovery is not authorization. ProductManager selects catalog ids in `blueprint.functions`; the backend derives installed user-function names in the caller manifest. A caller manifest contains:

```json
{
  "function_requirements": ["normalize_text"]
}
```

For Task DAG builds, ProductManager assigns approved catalog ids to nodes through `function_ids`; the backend gives each Builder full context only for that node. Single-Codex receives full context for every selected function. Function and service code use the trusted `function_runtime_capabilities.call_function` helper. Web-application server code uses `web_runtime_capabilities.call_function`. Browser code never receives either capability token.

Integration calls use the parallel stable helper `integration_runtime_capabilities.call`. The operation must be literal, declared by the active manifest, and approved for the caller's current provider fingerprint; typed GitHub repository identities must remain within exact scope, while existing provider-owned Notion, Calendar, Gmail, Telegram, and Atlas containment remains unchanged. The helper carries no credential. Policy derives the resource identity from validated input and rechecks current state before issuing the runtime object; provider adapters retrieve credentials only after that point and return bounded normalized output plus explicit audit metadata.

Function skills and scheduled services call the selected `backend.codex.call` capability through `function_runtime_capabilities.call_codex`. The helper sends the ephemeral bounded-run bearer token through the private relay; the backend resolves the caller and active version from that token rather than trusting a submitted skill id. A service token is accepted only for a schedule-attributed service run. Callers may optionally provide a JSON `response_schema`; the backend validates it and passes it to Codex as the bounded output contract before returning the response. Codex call/response and optional internet access remain governed by the active manifest and approved runtime permissions. The legacy `POST /skills/{skill_id}/codex` route remains available to trusted local callers for compatibility but is not the sandbox transport.

For concise extension blueprints covering user, integration, and backend-core functions, see [Extending the function catalog](function_extension_guide.md).

The trusted integration path also includes Google Calendar, Gmail, and Telegram. Gmail and Calendar keep separate OAuth identities; direct provider access remains blocked.

Approval-required functions and integrations expose a backend-derived contract consistently in catalog, ProductManager context, capability discovery, manual runs, and MCP. For integrations, canonical `HIGH` risk is the sole trigger; callers and adapters cannot suppress it. The immediate caller receives only a pending approval id and is never resumed. Execution happens later through the backend-owned decision service and re-enters the same policy path. See [Invocation approvals](../security/invocation_approvals.md).

## Authorization

The backend evaluates the direct caller-to-target relationship on every call:

- the caller identity comes from an ephemeral capability tied to the currently running caller version or from a current web-application instance capability;
- the caller and target must remain installed, enabled, version-current, and normally runnable;
- the active caller manifest must explicitly declare the target;
- input JSON must satisfy the target input schema;
- low-risk targets need no extra caller-specific approval;
- medium- and high-risk targets need an approved caller-target relationship;
- unsupported or blocked target permissions remain unavailable regardless of relationship approval.

Function-access approval is specific to one caller skill and one target function. Its fingerprint covers target risk, permissions, dependencies, input/output schemas, and the target's transitive function graph. Code-only target version changes retain approval; a changed fingerprint makes the relationship stale and requires review. Approval does not authorize other callers. The parent's runtime review inherits the transitive permission union, while execution still gives each process only its own manifest permissions.

Direct user runs and backend actions keep their existing authorization boundaries. Trusted backend callers use a named system context but receive no generic approval bypass. A scheduled service receives caller authority only for its schedule-attributed run. None of these paths creates a synthetic caller skill. Function-target paths converge through `InvocationExecutor`; the user-function handler coordinates registry availability checks, operation locking, bounded execution, and audit attribution.

## Runtime and Audit

The target still runs through the disposable function runner and creates its normal `skill_runs` record. Runs record target version, invocation source, caller skill/version, parent run id when applicable, schedule id or web-app instance when applicable, and a bounded initiating-action label. Runtime Codex usage remains attached to that target run and separate from Project build totals.

No-internet Docker callers receive backend Function and integration capability access through a transient allowlisted relay on an internal Docker network. The caller cannot use those exact paths for general backend access or internet egress. Functions with approved runtime network domains continue to use the existing bridge behavior, but direct GitHub, Atlas, and Notion access is still prohibited; domain-level egress filtering remains separately disclosed.

Input mismatch blocks before entrypoint execution and is audited as a blocked target run. Output mismatch changes the completed target run to failed while preserving its output and process diagnostics.

Functions invoked by a service, function, or web application receive a new scoped capability while their target run is active. The backend validates the complete directed graph as finite and acyclic; there is no arbitrary numeric depth cap. Every edge independently rechecks the immediate caller's active manifest, caller and target versions, target availability, input/output schemas, caller-specific approval when required, per-skill operation lock, and the target's own permissions and integration approvals. Capabilities expire with their owning run. Services remain scheduler-only and cannot become call targets.

Each node keeps its ordinary bounded runner timeout and resource limits. A child failure is returned synchronously to its immediate caller; callers decide whether that makes their own result fail. There is no separate chain-wide cancellation token or shared resource pool. Parent/child run ids provide the complete audit chain, while the graph fingerprint makes descendant risk, permission, availability, and contract changes stale before execution. Disabling a child makes every transitive parent unavailable; deleting or invalidating a child places every transitive parent in the explicit `error` availability state until its declaration is repaired.

## Compatibility

Manual bounded function runs, scheduled service runs, active-version switching, runtime permissions, and existing run history remain supported. Older function packages without JSON schemas remain directly runnable but cannot be used as registry targets until updated. Packages remain exactly one runtime protocol; a service or web application calling a declared function does not become a hybrid package.
