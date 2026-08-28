# Codex MCP Tools

Eidolon can register one local STDIO MCP server in the host-level Codex configuration. The ChatGPT desktop app, Codex CLI, and Codex IDE extension share that host configuration. Installation is explicit from **Settings → Integrations → Codex tools**; Eidolon chat does not receive these tools.

## Discovery Contract

The MCP process initializes the database without starting FastAPI, schedulers, Atlas lifecycle management, Codex usage monitoring, or web-application maintenance. At process startup it snapshots every currently `available` catalog entry that is eligible for MCP:

- integration operations are derived automatically from `integration_registry.py`;
- installed enabled user functions are derived automatically from their active manifest;
- `backend.codex.call` has `mcp_exposed: false` and is excluded to prevent recursive Codex invocation;
- future backend-core entries remain excluded until they have an explicit trusted direct handler.

Tool names are stable and have the form `<category>_<normalized-id>_<8-character-id-hash>`. Each tool preserves the catalog title, description, input schema, output schema, and risk-derived safety context. Integration read-only status comes from `IntegrationOperation.read_only`; `notion.todo.delete` and `notion.report.delete` are destructive. User functions default to write-capable because their metadata does not prove semantic purity. GitHub, Notion, and networked user functions are marked open-world.

There is no generic function-id dispatcher tool. Catalog additions appear when a new Codex session starts; an already-running session keeps its snapshot. Every call rechecks the enabled-state row, current availability, and the snapshotted callable contract. A changed contract fails with a bounded restart-required error instead of running against stale metadata.

## Invocation and Safety

Installed user functions call `FunctionRegistryService.invoke_direct` with `invocation_source="codex_mcp"`. Runtime approval, input/output validation, per-skill operation locking, run history, active-version checks, and declared nested integration/function capabilities remain enforced.

Integration tools use the trusted direct-user path in `IntegrationService`. It omits only skill-specific manifest authorization. Provider connection checks, schema validation, GitHub and Atlas boundaries, separate Notion Todo/Reports data-source containment, credential isolation, timeouts, provider response limits, output validation, and normalized errors remain in the existing trusted provider services. GitHub direct tools may use any repository allowed by the configured token; generated skills keep their manifest repository scopes.

Provider credentials stay in Windows Credential Manager. MCP results return exact validated object output as `structuredContent` plus a short non-sensitive text summary. Generic MCP request and response bounds apply in addition to provider/runtime bounds.

`mcp_audit_records` stores only caller type, function ID, category, status, a bounded resource identifier when applicable, normalized error type, byte counts, and timestamps. It never stores prompts, arguments, results, credentials, secret references, headers, or capability tokens.

## Registration

The settings API is:

```text
GET    /settings/codex-mcp
PUT    /settings/codex-mcp   { "action": "install" | "repair" }
DELETE /settings/codex-mcp
```

Installation adds only this structure-preserved table to `CODEX_HOME/config.toml`, or `~/.codex/config.toml` when `CODEX_HOME` is unset:

```toml
[mcp_servers.eidolon]
command = "<active Eidolon backend Python>"
args = ["-m", "app.mcp_server"]
cwd = "<absolute Eidolon backend directory>"
enabled = true
required = false
default_tools_approval_mode = "writes"
startup_timeout_sec = 15
tool_timeout_sec = 180
```

No `enabled_tools` allowlist is written, so restarted sessions discover newly available catalog functions. Unrelated TOML, comments, plugins, and MCP servers are preserved. Eidolon stores a fingerprint of the exact table it wrote; install, repair, and remove refuse a conflicting table rather than taking ownership of it. Installation uses atomic replacement and restores the original file if verification or database persistence fails. Removal disables MCP calls in the database before touching the file, immediately revoking already-running MCP processes.

The configuration contains no provider credential, secret reference, header, or capability token. Codex uses `default_tools_approval_mode = "writes"`, so tools not marked read-only require confirmation. Restart Codex Desktop, CLI, or IDE sessions after installation, repair, or catalog changes.
