# Function Registry and Invocation

`runtime = function` is the machine-facing, one-shot JSON protocol. Installed functions do not host HTTP services and are not added to the static trusted backend API catalog. The backend owns a separate dynamic registry for installed, user-controlled capabilities.

## Registry Contract

`GET /functions` returns installed function records with backend-validated identity, description, active version, input/output JSON Schemas, derived risk, effective permissions, and availability reasons. Registry results never expose package paths, entrypoint commands, credentials, container identities, or unrelated lifecycle state.

An installed function is available for registry invocation only when it is enabled, has a current active function version, has valid object-shaped input/output schemas, has approved and supported runtime permissions, and its active manifest matches the backend identity. Disabled or otherwise unavailable functions remain discoverable with explicit reasons.

Discovery is not authorization. A caller manifest must contain:

```json
{
  "function_requirements": [
    {
      "name": "normalize_text",
      "reason": "Normalize user-provided text before analysis."
    }
  ]
}
```

Function code uses the trusted `function_runtime_capabilities.call_function` helper. Web-application server code uses `web_runtime_capabilities.call_function`. Browser code never receives either capability token.

GitHub integration calls use the parallel stable helper `integration_runtime_capabilities.call`. The operation must be literal, declared by the active manifest, approved for the caller's current integration fingerprint, and within exact repository scope. The helper carries no credential; the backend retrieves it only after all invocation checks and performs the provider request. See [GitHub integration capability](../integrations/github.md).

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

No-internet Docker callers receive backend Function and integration capability access through a transient allowlisted relay on an internal Docker network. The caller cannot use those exact paths for general backend access or internet egress. Functions with approved runtime network domains continue to use the existing bridge behavior, but direct GitHub access is still prohibited; domain-level egress filtering remains separately disclosed.

Input mismatch blocks before entrypoint execution and is audited as a blocked target run. Output mismatch changes the completed target run to failed while preserving its output and process diagnostics.

Nested function calls are intentionally blocked in this milestone. The deferred design work is tracked in `TODO-013`.

## Compatibility

Manual bounded runs, approved schedules, active-version switching, runtime permissions, and existing run history remain supported. Older function packages without JSON schemas remain directly runnable and schedulable, but cannot be used as registry targets until updated. Packages remain exactly one runtime protocol; a web application calling a function does not become a hybrid package.
