# Permission System

The permission system is deterministic backend logic. It is intentionally not delegated to Codex.

## Approval Scopes

### Build-Time

Build-time approval lets Codex generate proposed files or draft update files. It does not approve installation, runtime permissions, package installation at runtime, schedules, or automatic execution.

For DAG builds, ProductManager returns build-time intent and expected runtime intent as structured JSON after `blueprint.json` exists and before the task DAG is created. The backend writes `permissions.json`, reads `blueprint.json` and `permissions.json`, performs deterministic review, and presents one build-time approval prompt with the blueprint summary plus permission summary. ProductManager must not return task DAG JSON until this approval is granted; the backend writes `task_dag.json` after approval.

ProductManager returns only permissions that need user approval. Backend-owned defaults and blocked capabilities live in `backend/app/static/default_permissions.json`. After build-time approval, the backend rewrites `permissions.json` as one minimal effective permission object with defaults already merged into its build-time and runtime fields. Builder receives that object once as compact `permission_bounds`; the static defaults and blocked policy are not duplicated into `permissions.json`.

### Runtime

Runtime approval is based on the actual generated `manifest.json`. It is required before install/run when permissions or dependencies require review.

### Schedule

Schedule approval activates an application schedule. It does not bypass runtime permission checks.

## Risk Levels

```text
low, medium, high, blocked
```

Examples:

- Low: no requested permissions, explicit public domains, own `./cache` read/write.
- Medium: package dependencies, web scraping, scheduled jobs.
- High/blocked in MVP: secrets, shell, arbitrary file access, broad writes, dangerous third-party actions.

## Supported Runtime Permissions

Allowed:

```json
{
  "build_time": {
    "internet_research": false,
    "dependencies": ["pytest", "requests"],
    "project_read": ["personal-agent"]
  },
  "runtime": {
    "python_standard_library": true,
    "network": ["explicit-domain.example"],
    "filesystem_read": ["./cache"],
    "filesystem_write": ["./cache"],
    "secrets": [],
    "shell": false,
    "codex": {
      "call_response": true,
      "internet_access": false
    },
    "dependencies": []
  }
}
```

`network` may also be empty. `filesystem_read` and `filesystem_write` may be empty.

Important limitation: approved network domains currently enable container network access but are not domain-firewalled. The UI must disclose this.

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
- email/calendar/finance actions;
- purchases;
- trading;
- public posting;
- file deletion.

## Static Capability Validation

Every completed Project build passes through the same backend-owned static capability scan before the backend runs generated tests and creates runtime permission review. The scanner inspects generated implementation Python while excluding tests, installed dependency code, caches, and metadata. DAG Tester may already have authored a final test file, but test authoring is outside this deterministic validator.

Recognized evidence includes:

- imports of selected network clients and literal HTTP(S) domains;
- direct process-execution APIs;
- selected browser-automation imports;
- literal filesystem writes outside approved runtime write paths;
- absolute or parent-traversing literal file access;
- sensitive environment-variable names;
- direct file-deletion APIs;
- Python files that cannot be parsed or exceed the bounded scan size.

Allowed network evidence is recorded when the actual generated manifest declares runtime network domains. A literal URL domain must match a manifest-declared domain. Recognized undeclared or blocked evidence fails final validation and prevents runtime permission review. A single-Codex workflow blocks immediately; a DAG workflow may use its bounded Builder repair loop and rescan. Results are persisted as `capability_scan.json` in the agent-run artifacts. The later runtime review separately compares the actual manifest with the earlier approved plan and requests approval for meaningful expansion.

This scan does not grant permissions and does not replace sandbox enforcement. It cannot reliably analyze dynamic imports, reflection, encoded source, dependency internals, runtime-built paths or domains, non-Python executables, or behavior hidden behind external services. Absence of a finding is not proof that code has no side effects.

## Permission Expansion

Runtime permission review compares actual manifest permissions/dependencies against the approved build-time plan. Meaningful expansion requires explicit runtime review. Empty expansion should not be displayed as a warning.

Default Codex call/response is not treated as a permission expansion. New `codex.internet_access=true` is expansion unless it was already planned through runtime network access.
