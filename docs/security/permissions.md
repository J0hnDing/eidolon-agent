# Permission System

The permission system is deterministic backend logic. It is intentionally not delegated to Codex.

## Approval Scopes

### Build-Time

Build-time approval lets Codex generate proposed files or draft update files. It does not approve installation, runtime permissions, package installation at runtime, schedules, or automatic execution.

For DAG builds, ProductManager writes build-time intent and expected runtime intent into `permissions.json` after `blueprint.json` exists and before the task DAG is created. The backend reads `blueprint.json` and `permissions.json`, performs deterministic review, and presents one build-time approval prompt with the blueprint summary plus permission summary. ProductManager must not create `task_dag.json` until this approval is granted.

### Runtime

Runtime approval is based on the actual generated `manifest.json`. It is required before install/run when permissions or dependencies require review.

### Schedule

Schedule approval activates an application schedule. It does not bypass runtime permission checks.

## Risk Levels

```text
low, medium, high, blocked
```

Examples:

- Low: no permissions, explicit public domains, own `./cache` read/write.
- Medium: package dependencies, web scraping, scheduled jobs.
- High/blocked in MVP: secrets, shell, arbitrary file access, broad writes, dangerous third-party actions.

## Supported Runtime Permissions

Allowed:

```json
{
  "network": ["explicit-domain.example"],
  "filesystem_read": ["./cache"],
  "filesystem_write": ["./cache"],
  "secrets": [],
  "shell": false,
  "codex": {
    "call_response": true,
    "internet_access": false
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

## Permission Expansion

Runtime permission review compares actual manifest permissions/dependencies against the approved build-time plan. Meaningful expansion requires explicit runtime review. Empty expansion should not be displayed as a warning.

Default Codex call/response is not treated as a permission expansion. New `codex.internet_access=true` is expansion unless it was already planned through runtime network access.
