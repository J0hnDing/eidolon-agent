# Notion Todo Integration

Eidolon uses one user-configured Notion data source as the sole todo datastore. Notion, including the Notion iOS app, is the only todo-management UI. Eidolon has connection controls only: it does not have a todo page, local todo table, cache, synchronization process, webhook, background queue, or offline copy.

## Notion Setup

Create a private/internal Notion connection with **read content**, **insert content**, and **update content** capabilities. Manually share the Todo database with that connection. Do not use OAuth or a broad personal token.

Create one database/data source with this exact property schema:

| Todo field | Notion property | Type |
| --- | --- | --- |
| `id` | none | Notion page ID |
| `title` | `Title` | Title |
| `done` | `Done` | Checkbox |
| `priority` | `Priority` | Select with exactly `Low`, `Medium`, and `High` |
| `start_at` | `Start At` | Date |
| `due_at` | `Due At` | Date |
| `estimated_minutes` | `Estimated Minutes` | Number |
| `atlas_goal_id` | `Atlas Goal ID` | Rich text |
| `notes` | `Notes` | Rich text |
| `created_at` | `Created At` | Created time |

All editable properties except `Title` are optional. `Done` is always returned as a boolean and defaults to unchecked when omitted during creation. A title-only row therefore works naturally in Notion mobile, and `Created At` is populated automatically. Arrange database views manually in Notion; Eidolon never creates, migrates, renames, or continuously reconciles the schema.

Copy the data-source ID from **Manage data sources** in Notion, then open Eidolon Settings and submit the write-only connection token plus that ID. Connection save retrieves the bot identity and data source, verifies access and the exact property types/options, and only then atomically switches the Windows Credential Manager entry. A failed replacement preserves the prior connection. SQLite stores only the opaque secret reference, sanitized bot/workspace identity, configured data-source ID, status, and timestamps.

Notion's current data-source API and the pinned `Notion-Version: 2026-03-11` contract are documented in [Retrieve a data source](https://developers.notion.com/reference/retrieve-a-data-source), [data-source property types](https://developers.notion.com/reference/property-object), [authorization](https://developers.notion.com/guides/get-started/authorization), and [connection capabilities](https://developers.notion.com/reference/capabilities).

## Normalized Contract

The registry exposes exactly four generated-skill operations:

| Operation | Risk | Input and result |
| --- | --- | --- |
| `notion.todo.list` | low, read-only | Optional `page_size` 1-100 and `start_cursor`; returns newest-created `todos`, `has_more`, and `next_cursor`. Trashed pages are excluded. |
| `notion.todo.create` | medium write | Requires a non-empty `title`; all other mutable fields are optional; returns the normalized todo. |
| `notion.todo.update` | medium write | Requires `id` and at least one mutable field. Omitted fields are unchanged and explicit `null` clears an optional field. |
| `notion.todo.delete` | medium write | Requires `id`, sets `in_trash: true`, and returns `{id, removed: true}`. |

`id` is the Notion page ID, `done` is the `Done` checkbox state, and `created_at` is the immutable page creation timestamp. Create and update accept `done`; list returns it for every todo. `atlas_goal_id` is an opaque optional string. Atlas does not know about Notion, and every todo operation remains independent of Atlas availability.

The list request does not send `in_trash` in its query body because Notion rejects that parameter for this data-source query. The provider requires every returned page to contain a boolean `in_trash` value and excludes pages where it is `true`, so malformed or trashed rows cannot enter the normalized result. Delete uses `in_trash: true` only in the supported page-update body and requires the returned page to confirm the boolean value.

Notion does not provide permanent page deletion through the API. In this contract, delete and completion both mean moving the page to trash. See [Notion trash semantics](https://developers.notion.com/reference/trash-page).

## Scheduled Done Cleanup

The scheduler automatically registers the trusted backend-core function `backend.notion.todo.cleanup_done` to run daily at 03:00 `America/Toronto`. It follows every `next_cursor` within a 100-page bound, moves only todos with `done: true` to trash, continues processing after an individual deletion failure, and returns bounded deleted IDs and failure details. A disconnected or invalid Notion connection fails safely and is tried again on the next daily run.

This function is always unavailable in the ordinary function catalog and is not exposed to ProductManager, generated skills, direct API callers, or Codex MCP. Only the scheduler-owned backend dispatcher may invoke it. It is a platform job rather than a `SkillSchedule`, so it does not require skill/runtime/schedule approval records. The Schedules page includes a read-only view of the registered platform job with its next and last run state.

## Trust and Resource Boundary

Generated function code calls only:

```python
integration_runtime_capabilities.call(
    operation="notion.todo.create",
    input={"title": "Buy groceries"},
)
```

It never receives the token, authorization headers, Notion URLs, configured data-source ID, secret reference, settings routes, or a generic HTTP client. `NotionTodoProvider` alone owns fixed HTTPS requests, `Notion-Version`, pagination, property mapping, containment, schema validation, response bounds, redirect rejection, timeouts, and error normalization.

The configured data source is the complete resource boundary. List queries only it; create fixes it as the parent; update and delete retrieve the page first and return `not_found` unless `parent.data_source_id` matches. Callers cannot select another source or depend on Atlas.

Direct `api.notion.com` access, authentication construction, sensitive Notion environment reads, settings routes, and browser-side integration calls are blocked by generated-code validation and runtime network controls. Provider errors are bounded to `invalid_input`, `invalid_credential`, `provider_forbidden`, `not_found`, `schema_mismatch`, `rate_limited`, `provider_timeout`, `response_too_large`, `provider_unavailable`, and existing authorization/connection failures. Rate limits are returned immediately; bounded valid `Retry-After` seconds are surfaced to the caller rather than queued or retried in the background. See [Notion request limits](https://developers.notion.com/reference/request-limits).

## Connection and Skill Approval

A connected Notion data source does not authorize a skill. The active manifest must select exact operations, and Eidolon creates the existing separate `integration_access` approval. List is low-risk/read-only. Create, update, and delete are medium-risk writes. Changing the validated bot identity or configured data-source ID supersedes existing Notion authorizations; unchanged contracts retain their normal fingerprint behavior.

ProductManager receives the normal concise available-function cards only. Builder, Tester, and single-Codex receive normalized schemas, examples, risk, helper instructions, error contracts, and deterministic fake guidance only for selected Notion functions. Credentials and connection configuration never enter prompts, generated packages, containers, outputs, logs, audits, or artifacts.

## Trusted Settings API

- `GET /settings/integrations/notion`: sanitized connection status.
- `PUT /settings/integrations/notion`: write-only `token` plus `data_source_id`, validated before atomic replacement.
- `DELETE /settings/integrations/notion`: removes the active Windows credential and connection row.

There are no public todo-operation routes. Installed skills use the existing hidden runtime relay after manifest, approval, connection, schema, and containment enforcement.
