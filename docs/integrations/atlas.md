# Eidolon-Atlas Integration

Eidolon treats a local Eidolon-Atlas instance as a trusted integration provider. Atlas remains the authority for encryption, unlock state, its native loopback API, record and Knowledge operations, and the invariants enforced by each primitive write. Eidolon owns agent-oriented filtering, Knowledge search/frontier derivation, bounded projections, and Codex orchestration. Generated skills never contact Atlas directly; they call selected registry operations through Eidolon's integration capability.

## Lifecycle and Settings

FastAPI startup attempts to attach to a compatible Atlas already listening on `127.0.0.1:4817`, or starts `node src/index.js` from the configured Atlas directory. The default is the sibling `Eidolon-Atlas` checkout. A custom directory must be absolute, contain `package.json` and `src/index.js`, and run with Node.js 24 or newer. Atlas startup failure never prevents Eidolon startup, and Eidolon stops only a process it owns.

Settings exposes directory and process state, initialized/locked state, optional automatic-unlock state, bounded errors, restart, passphrase management, and explicit unlock. Changing directories immediately restarts Atlas, clears its saved passphrase, and invalidates Atlas authorizations.

Atlas functions require no API key or stored connection. They are available whenever the selected Atlas instance is running and unlocked. An optional passphrase is stored as the Atlas connection's primary Windows Credential Manager credential; SQLite stores only its opaque reference and sanitized status metadata. Eidolon may save, replace, or send that passphrase only when it owns the Atlas process. At later owned-process startups, Eidolon makes one automatic unlock attempt. Manual Atlas Lock remains effective until restart or explicit **Unlock now**.

An externally launched Atlas is usable without a saved passphrase when it is unlocked. When it is locked, it must be unlocked through Atlas itself; Eidolon never sends a stored or newly entered passphrase to an external process. Removing a saved passphrase disables automatic unlock without disabling an already-unlocked Atlas or invalidating function authorization.

On upgrade, any legacy Atlas API-key connection and its associated passphrase are deleted and existing Atlas authorizations are invalidated. If Credential Manager is unavailable, Eidolon retains only the cleanup references, never reads or uses them, reports cleanup as pending, and retries before accepting a new passphrase.

Storing the passphrase makes the Windows account the practical at-rest security boundary. There is no plaintext, environment-variable, config-file, SQLite-secret, application-encrypted, or backup fallback. Eidolon does not initialize Atlas, change or recover its passphrase, or use it for Atlas backups.

## Read capability

Atlas reads are backend-owned integration functions in the ordinary catalog, available to Act, MCP, and explicitly declared skill/service callers through the same invocation, authorization, schema validation, and audit path. They do not create a generated skill or provide arbitrary HTTP access. Agents should discover the schema, search compact identities, and fetch relevant details as needed.

| Operation | Read coverage |
| --- | --- |
| `atlas.describe` | `overview` returns native schemas and read guidance; `api_reference` returns OpenAPI, optionally one documented `path`; `model` returns model/End/resource catalogs; `custom_fields` returns definitions, optionally by category. Discovery does not authorize any additional operation. |
| `atlas.records.search` | All current categories, optional `query` and `trashed=false|true|all`; compact identity results by default, complete native records with `include_data=true`. |
| `atlas.records.get` | Complete native record by `id`: data, custom fields, provenance, facts, links and backlinks. This explicit read includes sensitive Person identifiers and physical health. |
| `atlas.links.get` | Complete native link by `id`. |
| `atlas.person.completeness.get` | Atlas's overall and per-area `complete`/`missing` percentages, counts/targets, criteria and available missing-field/domain/spectrum lists. |
| `atlas.states.get` | Complete state estimates, confidence and recency, optionally `at`, or `view=configuration`. Reads do not save observations or evolve stored state. |
| `atlas.personality.get` | Complete native Personality response, standalone spectra and exact imported Valence model when present; unknown values and stored precision stay unchanged. |
| `atlas.history.list` | `kind=record|link` revisions with `id`, or `kind=states` event/manual-estimate history. |
| `atlas.graph.get` | Full motivational graph, optionally focused by `goal_id`. |
| `atlas.knowledge.read` | Complete paged native `view=nodes`, one `view=node` with `node_id`, or `view=tree` including connections. |
| `atlas.attachments.list` | Image or resume metadata by `record_id`. |
| `atlas.attachments.read` | Explicit image/resume content reads. `kind=image` uses image ID; `kind=resume` uses Person record ID. Default base64 bytes are chunked; `format=text` extracts PDF/DOCX resume text. Nothing is saved to disk. |

Native data objects preserve new fields automatically rather than projecting only known legacy fields. Search matches native title/data/custom values. Category strings are validated by Atlas, so new native record categories need no Eidolon enum update. New subsystems with different routes still require a deliberate backend adapter addition.

Record searches and history return `items`, `total`, and `next_cursor`; native Knowledge node pages return `data`, `total`, and `next_cursor`. Page sizes are 1–100 and byte-bounded. Cursors bind the source snapshot and query; source changes return `stale_revision` and the caller starts again. States history uses native pagination with a first-event/count anchor. Attachment chunks return `next_offset` and `sha256`; supply that hash on later chunks to detect replacement. Native JSON reads are bounded at 32 MiB, collection pages at 512 KiB, and binary reads at 20 MiB (Atlas's image maximum). Oversized responses fail explicitly; narrow the search, fetch a record separately, reduce page size, or use paged Knowledge nodes. Completeness uses the exact shared Atlas Overview calculation: six equally weighted areas (core profile, Goals, Experiences, Relationships, Beliefs, Personality), with `asOfDate` and `definition` metadata. It is a coverage estimate, not a prediction of personal wellbeing.

Transport limits still apply after provider reads: MCP output is limited to 1 MiB and skill integration responses to 5,000,000 bytes. A native response fitting the provider limit can exceed a caller's transport limit; the caller receives `response_too_large`, never a silently shortened record.

## Existing semantic functions

Stable operation IDs remain available with current contracts:

- `atlas.person.get` retains existing profile names and resume metadata, adds complete non-identifier `data`, `custom_field_values`, and top-level `completeness`. Government identifiers remain excluded from this convenience projection; complete record reads are explicit.
- `atlas.interest.get` / `atlas.interest.list` return `interests` with current numeric `liking` and `skill_level`, native data and custom fields. Interests remain distinct from Ends categorized as interests; the old hobbies/preferences projection is removed.
- Experience, Project and Relationship lists retain their filters and meaningful projection fields, adding record identity/revision and complete `data` / `custom_field_values`.
- `atlas.goal.list` returns flat `goals` plus the native motivational `graph`, with optional `horizon`, `pursuit_status`, and `limit`. Progress is a nullable number derived from blueprint steps, preserving fractional values; an empty blueprint is unassessed.
- `atlas.goal.create` and `atlas.goal.update` accept current Goal fields: title, description, success conditions, pursuit status, commitment, horizon, target date, blueprint, provenance and custom fields. Updates require `expected_revision`; `target_date:null` clears the date. Atlas validates the blueprint DAG. Obsolete subgoal, importance, Result, explicit progress, prerequisite and request-ID inputs are rejected. Create is not idempotent.
- Knowledge frontier/search/bounded-node functions and `atlas.knowledge.node.know` retain their bounded semantics.

Breaking contract changes increment versions and invalidate old runtime approval fingerprints. Existing skills must review changed Atlas permissions before running again; the backend never rewrites approvals to authorize new read access. Generated skills must explicitly declare selected new read operations. The catalog automatically publishes the registry on backend startup; restart existing MCP sessions to refresh their snapshotted tool contracts.

Reads are low risk. Goal create/edit and `atlas.knowledge.node.know` remain medium risk. The latter makes one internet-enabled Codex call and may mark one node known/add immediate name-only unassessed children; it cannot rename, move, delete, merge, or recursively expand nodes.

Atlas functions are offered to ProductManager whenever Atlas is running and unlocked. Secret-store availability and a saved passphrase do not gate them. `Know_node` additionally requires a compatible Codex CLI. Atlas has an empty manifest resource scope; GitHub continues to use exact repository scopes.

## `Know_node` Context Boundary

Eidolon authorizes the caller and fetches one bounded node before Codex runs. An already-known node fails immediately. Codex receives only target identity/status/revision, canonical path, parent summary, immediate child names/statuses, the expansion rules, and the optional caller explanation. It never receives credentials, other Atlas record categories, unrelated Knowledge nodes, or the function catalog.

Live search is restricted to the public topic. A supplied explanation is authoritative, preserved after surrounding-whitespace normalization, and never searched, evaluated, or rewritten; Codex generates only terms and immediate child names. Without one, Codex also generates a direct explanation. Strict structured output is validated before Eidolon rechecks the observed revision, patches the target through the primitive Knowledge API, and creates missing immediate children through primitive create calls. Codex failure or invalid output performs no write. A stale revision detected before the patch performs no write and is not silently retried.

The primitive Atlas API makes the target patch and each child creation individually atomic; it does not provide a transaction spanning the full `Know_node` sequence. A later child-creation failure can therefore leave the target known and any earlier children created. Eidolon reports that failure and does not retry silently. This is the deliberate consequence of keeping Atlas primitive rather than adding an orchestration-specific endpoint.

Integration audit records contain caller/version attribution, operation, selected node ID when applicable, status, normalized error type, timestamps, and bounded sizes. They never contain personal content, explanations, terms, children, prompts, raw responses, exceptions, or secrets.
