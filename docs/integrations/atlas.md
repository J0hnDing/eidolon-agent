# Eidolon-Atlas Integration

Eidolon treats a local Eidolon-Atlas instance as a trusted integration provider. Atlas remains the authority for encryption, unlock state, its read-only Bearer API, primitive record and Knowledge operations, and the invariants enforced by each primitive write. Eidolon owns agent-oriented filtering, Knowledge search/frontier derivation, bounded projections, and Codex orchestration. Generated skills never receive Atlas credentials or contact Atlas directly; they call selected registry operations through Eidolon's integration capability.

## Lifecycle and Settings

FastAPI startup attempts to attach to a compatible Atlas already listening on `127.0.0.1:4817`, or starts `node src/index.js` from the configured Atlas directory. The default is the sibling `Eidolon-Atlas` checkout. A custom directory must be absolute, contain `package.json` and `src/index.js`, and run with Node.js 24 or newer. Atlas startup failure never prevents Eidolon startup, and Eidolon stops only a process it owns.

Settings exposes directory and process state, initialized/locked state, API-key state, automatic-unlock state, bounded errors, restart, key management, passphrase management, and explicit unlock. Changing directories immediately restarts Atlas, clears the old key and passphrase, and invalidates Atlas authorizations.

The Atlas API key and optional passphrase are separate Windows Credential Manager entries. SQLite stores only opaque references and sanitized status metadata. Initial setup and one manual Atlas unlock are required before Eidolon can validate a key and store a passphrase. At later startups, Eidolon makes one automatic unlock attempt. Manual Atlas Lock remains effective until restart or explicit **Unlock now**.

Storing the passphrase makes the Windows account the practical at-rest security boundary. There is no plaintext, environment-variable, config-file, SQLite-secret, application-encrypted, or backup fallback. Eidolon does not initialize Atlas, change or recover its passphrase, or use it for Atlas backups.

## Function Catalog

The provider exposes these fixed operations:

- `atlas.person.get`: non-sensitive built-in Person projection.
- `atlas.experience.list`: optional title/description keywords, ongoing state, and limit.
- `atlas.goal.list`: optional importance and horizon filters on top-level goals; retained goals include their complete subgoal trees and progression edges.
- `atlas.project.list`: optional title/description keywords, status, GitHub-link presence, and limit.
- `atlas.relationship.list`: optional name/type/notes keywords, kind, status, importance, and limit.
- `atlas.knowledge.frontier.list`: paged Subjects-only assessment frontier.
- `atlas.knowledge.search`: ranked compact candidates by name, then known explanation and terms.
- `atlas.knowledge.node.get`: one bounded node with path, parent, immediate children, explanation, terms, status, and revision.
- `atlas.knowledge.node.know`: one explicit medium-risk Knowledge establishment.

These are Eidolon function contracts, not one-for-one Atlas endpoints. Person, Experience, Goal, and Project use Atlas's four read-only Bearer projections. Relationships use the generic record list. Knowledge functions read the primitive flat node list and derive ranking, frontier membership, canonical paths, parents, and immediate children inside Eidolon. Atlas does not expose agent-specific Relationship, Knowledge search, frontier, inspection, or establishment routes.

Reads are low risk. `atlas.knowledge.node.know` is medium risk because it makes one internet-enabled Codex call, writes the selected Knowledge node, and may add immediate name-only unassessed children. It cannot rename, move, delete, merge, or recursively expand nodes.

Atlas functions are offered to ProductManager only when Atlas is running, unlocked, and the stored key validates. `Know_node` additionally requires a compatible Codex CLI. Atlas has an empty manifest resource scope; GitHub continues to use exact repository scopes.

## `Know_node` Context Boundary

Eidolon authorizes the caller and fetches one bounded node before Codex runs. An already-known node fails immediately. Codex receives only target identity/status/revision, canonical path, parent summary, immediate child names/statuses, the expansion rules, and the optional caller explanation. It never receives credentials, other Atlas record categories, unrelated Knowledge nodes, or the function catalog.

Live search is restricted to the public topic. A supplied explanation is authoritative, preserved after surrounding-whitespace normalization, and never searched, evaluated, or rewritten; Codex generates only terms and immediate child names. Without one, Codex also generates a direct explanation. Strict structured output is validated before Eidolon rechecks the observed revision, patches the target through the primitive Knowledge API, and creates missing immediate children through primitive create calls. Codex failure or invalid output performs no write. A stale revision detected before the patch performs no write and is not silently retried.

The primitive Atlas API makes the target patch and each child creation individually atomic; it does not provide a transaction spanning the full `Know_node` sequence. A later child-creation failure can therefore leave the target known and any earlier children created. Eidolon reports that failure and does not retry silently. This is the deliberate consequence of keeping Atlas primitive rather than adding an orchestration-specific endpoint.

Integration audit records contain caller/version attribution, operation, selected node ID when applicable, status, normalized error type, timestamps, and bounded sizes. They never contain personal content, explanations, terms, children, prompts, raw responses, exceptions, or secrets.
