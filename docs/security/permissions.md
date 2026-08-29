# Permission System

The permission system is deterministic backend logic. It is intentionally not delegated to Codex.

## Approval Scopes

### Build-Time

For a new Project build, build-time approval lets the backend provision the listed Python dependencies before Codex starts and lets Codex generate proposed files. It does not approve skill installation, runtime permissions, package installation at runtime, schedules, or automatic execution. Generated code and agents remain prohibited from installing packages.

For DAG builds, ProductManager returns build-time intent and expected runtime intent as structured JSON after `blueprint.json` exists and before the task DAG is created. The backend writes `permissions.json`, reads `blueprint.json` and `permissions.json`, performs deterministic review, and presents one build-time approval prompt with the blueprint summary plus permission summary. ProductManager must not return task DAG JSON until this approval is granted; the backend writes `task_dag.json` after approval.

`backend/app/static/default_permissions.json` is the canonical agent-facing permission policy. Its three planning sections are `default_allowed`, `requires_approval`, and `blocked`. No agent instruction file contains a second permission list. Plausibility receives only `blocked`; blueprint and update planning receive the complete policy; post-approval ProductManager, Builder, Tester, repair, update, and single-Codex actions receive effective `permission_bounds` whose `blocked` field comes from the same config.

ProductManager returns the exact `requires_approval` shape only. Every ProductManager output-schema request regenerates the strict permission-plan JSON Schema from that config section; nested fields are required, unknown fields are rejected, and list values must be unique non-empty strings. Backend sanitization independently uses the same template and discards unknown permission fields. After build-time approval, the backend rewrites `permissions.json` as one minimal effective permission object with `default_allowed` merged into its build-time and runtime fields. The blocked policy is supplied in agent context rather than persisted into `permissions.json`.

The trusted Settings API exposes the current policy read-only at `GET /settings/permission-policy`, and the Settings page renders that response. The API and UI do not maintain another policy definition.

Immediately after approval, the backend creates a clean proposed-skill workspace and provisions approved runtime requirements into `.deps`. Missing build-only requirements are isolated in `.build-deps`; platform `pytest` availability is verified through the same backend interpreter. Both folders are placed on the Codex and authoritative-test `PYTHONPATH`, and the backend interpreter directory is first on `PATH`. Provisioning is atomic and reused after successful verification. Failure stops before any post-approval Codex invocation. `.build-deps` is never installed with the skill, while `.deps` is copied as part of the versioned runtime package.

### Runtime

Runtime approval is based on the actual generated `manifest.json`. Installation remains a separate decision, and neither an installed function run nor an installed web-application session may start until the corresponding runtime declaration is approved.

Declared `function_requirements` are shown during build-time and runtime review but are not permissions inherited from the target. Low-risk targets need no additional caller approval. Medium- and high-risk targets create a separate `function_access` approval tied to the caller and target. That approval is reusable only while the target risk, permissions, dependencies, and JSON callable schemas keep the same backend fingerprint. It never overrides a disabled target, missing runtime approval, unsupported permission, or blocked platform policy.

Provider availability and skill authorization remain separate facts. Every actual-manifest `integration_requirements` entry creates or reuses an auditable `integration_access` record, but the user reviews it together with the base manifest permissions in one complete runtime approval. One decision applies to every pending component shown. Each integration fingerprint includes provider, operations, normalized scope, and registry contract versions. Expansion, GitHub or Google account change, an Atlas native-contract or directory change, or a Notion bot identity change requires reapproval. Notion Todo and Reports source changes affect operation availability without invalidating authorization. Removing an Atlas auto-unlock passphrase does not invalidate authorization. See [GitHub integration](../integrations/github.md), [Atlas integration](../integrations/atlas.md), [Notion integration](../integrations/notion.md), and [Google Calendar integration](../integrations/google_calendar.md).

### Schedule

A service schedule can be resumed only after the current runtime and integration permission contract is approved. There is no separate schedule approval request.

## Risk Levels

```text
low, medium, high, blocked
```

Current examples, summarized from the canonical config and deterministic enforcement:

- Low: no requested permissions, own `./cache` read/write, or explicitly approved backend-mediated Codex call/response without internet.
- Medium: explicit public domains, package dependencies, web scraping, or Codex internet access.
- High/blocked in MVP: secrets, shell, arbitrary file access, broad writes, dangerous third-party actions.

## Supported Runtime Permissions

Canonical manifests declare approval-gated permissions only. Backend defaults such as Python standard-library use and own-`./cache` access are merged into the effective Builder and runner contract rather than copied into `manifest.json`. Blocked capabilities are policy constraints, not manifest declarations. For example:

```json
{
  "network": ["explicit-domain.example"],
  "codex": {
    "call_response": true,
    "internet_access": true
  }
}
```

`network` may be omitted. An empty manifest permission object requests no approval-gated runtime capabilities.

Important limitation: approved network domains currently enable container network access but are not domain-firewalled. The UI must disclose this.

For web applications, browser-side external traffic is not derived from `runtime.network` and is blocked entirely. Browser code must call same-origin application routes; approved server-side code may then use the declared network capability. Application ingress remains available through a private gateway channel even when runtime network domains are empty.

The backend web-app policy supports scripts, forms, isolated same-origin routes, modals, and approved server-side network/Codex access. It blocks external browser traffic, top navigation, popups, downloads, privileged browser features, and WebSockets.

Blocked:

- wildcard network;
- arbitrary filesystem reads;
- writes outside `./cache`;
- absolute paths or parent traversal;
- secrets;
- shell;
- Codex permissions other than `call_response` and `internet_access`;
- Codex internet access without approved runtime network domains;
- browser automation;
- direct email/calendar/finance actions outside a declared trusted integration operation;
- purchases;
- trading;
- public posting;
- file deletion outside approved `./cache` storage; cache-local replacement and cleanup are covered by cache write access.

## Static Capability Validation

Every completed Project build passes through the same backend-owned static capability scan before the backend runs generated tests and creates runtime permission review. The scanner inspects generated implementation Python plus literal browser URLs in HTML/CSS/JavaScript while excluding tests, installed dependency code, caches, and metadata. DAG Tester may already have authored a final test file, but test authoring is outside this deterministic validator.

Recognized evidence includes:

- imports of selected network clients and literal HTTP(S) domains;
- direct process-execution APIs;
- selected browser-automation imports;
- literal filesystem writes outside approved runtime write paths;
- absolute or parent-traversing literal file access;
- sensitive environment-variable names;
- literal file-deletion targets proven to be outside approved runtime write roots;
- Python files that cannot be parsed or exceed the bounded scan size.
- literal absolute HTTP(S) URLs in browser assets, which are blocked regardless of server-side domains.
- direct GitHub, Atlas, Notion, or Google traffic, provider authentication construction, secret-store access, sensitive integration environment reads, trusted integration Settings routes, Atlas passphrase/unlock/browser-Knowledge routes, direct internal integration paths, literal undeclared or unselected operation ids, capability serialization, and browser-side integration use.

Network imports alone are not findings. Network evidence is recorded only for recognized calls with literal domains, and a literal URL domain must match a manifest-declared domain. Import aliases are resolved, while local URL string helpers such as `urllib.parse` are not network evidence. Process imports likewise do not block unless a recognized execution API is called. File cleanup with a dynamic target is not treated as proof of out-of-bounds deletion; literal deletion is blocked only when its target is provably outside approved runtime write roots. Integration operation expressions that are not inline string literals are likewise ambiguous rather than findings; the runtime helper remains authoritative for manifest declaration, selected-operation authorization, and scope enforcement. Recognized undeclared or blocked evidence fails final validation and prevents runtime permission review. A single-Codex workflow blocks immediately; a DAG workflow may use its bounded Builder repair loop and rescan. Results are persisted as `capability_scan.json` in the agent-run artifacts. The later runtime review separately compares the actual manifest with the earlier approved plan and requests approval for meaningful expansion.

This scan does not grant permissions and does not replace sandbox enforcement. It is intentionally evidence-based rather than fail-closed when a path, domain, or call target cannot be proven statically. It cannot reliably analyze dynamic imports, reflection, encoded source, dependency internals, runtime-built paths or domains, Python-embedded browser assets, non-Python executables, or behavior hidden behind external services. Absence of a finding is not proof that code has no side effects.

Ordinary network approval never authorizes direct provider traffic. GitHub, Atlas, Notion, and Google Calendar calls must use the trusted helper so runtime scope, result limits, normalized output, and auditing remain enforceable.

## Permission Expansion

Runtime permission review compares actual manifest permissions/dependencies against the approved build-time plan. Meaningful expansion requires explicit runtime review. Empty expansion should not be displayed as a warning. The backend creates this review only for a finalized proposed or installed skill, fingerprints the validated manifest permission/dependency contract, and verifies that fingerprint again before installation or execution. If that contract changes, the old runtime request is superseded and a new decision is required.

Runtime review also resolves every declared function requirement against the current dynamic registry. Missing, disabled, schema-less legacy, permission-blocked, or otherwise unavailable targets are reported explicitly. Discovery alone never grants invocation authority.

New `codex.call_response=true` or `codex.internet_access=true` is a permission expansion unless it was present in the approved plan. Codex internet access also requires approved runtime network domains.
