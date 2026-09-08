# Eidolon-Atlas Integration

Eidolon treats a local Eidolon-Atlas instance as a trusted integration provider. Atlas remains the authority for encryption, unlock state, its native loopback API, record and Knowledge operations, and the invariants enforced by each primitive write. Eidolon owns agent-oriented filtering, Knowledge search/frontier derivation, bounded projections, and Codex orchestration. Generated skills never contact Atlas directly; they call selected registry operations through Eidolon's integration capability.

## Lifecycle and Settings

FastAPI startup attempts to attach to a compatible Atlas already listening on `127.0.0.1:4817`, or starts `node src/index.js` from the configured Atlas directory. The default is the sibling `Eidolon-Atlas` checkout. A custom directory must be absolute, contain `package.json` and `src/index.js`, and run with Node.js 24 or newer. Atlas startup failure never prevents Eidolon startup, and Eidolon stops only a process it owns.

Settings exposes directory and process state, initialized/locked state, optional automatic-unlock state, bounded errors, restart, passphrase management, and explicit unlock. Changing directories immediately restarts Atlas, clears its saved passphrase, and invalidates Atlas authorizations.

Atlas functions require no API key or stored connection. They are available whenever the selected Atlas instance is running and unlocked. An optional passphrase is stored as the Atlas connection's primary Windows Credential Manager credential; SQLite stores only its opaque reference and sanitized status metadata. Eidolon may save, replace, or send that passphrase only when it owns the Atlas process. At later owned-process startups, Eidolon makes one automatic unlock attempt. Manual Atlas Lock remains effective until restart or explicit **Unlock now**.

An externally launched Atlas is usable without a saved passphrase when it is unlocked. When it is locked, it must be unlocked through Atlas itself; Eidolon never sends a stored or newly entered passphrase to an external process. Removing a saved passphrase disables automatic unlock without disabling an already-unlocked Atlas or invalidating function authorization.

On upgrade, any legacy Atlas API-key connection and its associated passphrase are deleted and existing Atlas authorizations are invalidated. If Credential Manager is unavailable, Eidolon retains only the cleanup references, never reads or uses them, reports cleanup as pending, and retries before accepting a new passphrase.

Storing the passphrase makes the Windows account the practical at-rest security boundary. There is no plaintext, environment-variable, config-file, SQLite-secret, application-encrypted, or backup fallback. Eidolon does not initialize Atlas, change or recover its passphrase, or use it for Atlas backups.

## Function Catalog

The provider exposes these fixed operations:

- `atlas.person.get`: non-sensitive built-in Person projection.
- `atlas.interest.get` and `atlas.interest.list`: hobbies and preferences with only their meaningful labels and subtype fields; Atlas record metadata and the redundant subtype discriminator are excluded. New generated skills should use `atlas.interest.list`.
- `atlas.experience.list`: optional title/description keywords, ongoing state, and limit.
- `atlas.goal.list`: optional importance and horizon filters on top-level goals; retained goals include their complete subgoal trees and progression edges.
- `atlas.project.list`: optional title/description keywords, status, GitHub-link presence, and limit.
- `atlas.relationship.list`: optional name/type/notes keywords, kind, status, importance, and limit.
- `atlas.knowledge.frontier.list`: paged Subjects-only assessment frontier.
- `atlas.knowledge.search`: ranked compact candidates by name, then known explanation and terms.
- `atlas.knowledge.node.get`: one bounded node with path, parent, immediate children, explanation, terms, status, and revision.
- `atlas.knowledge.node.know`: one explicit medium-risk Knowledge establishment.

These are Eidolon function contracts, not one-for-one Atlas endpoints. Person, Interest, Experience, Goal, Project, and Relationship read native `/api/records` categories. Eidolon explicitly projects the existing bounded fields, excludes sensitive Person fields and Interest record metadata, preserves Experience and Project ordering, rebuilds Goal hierarchy, and fetches native Goal progression graphs for parent goals. Knowledge functions read the primitive flat node list and derive ranking, frontier membership, canonical paths, parents, and immediate children inside Eidolon. Atlas exposes no agent-specific endpoints.

Reads are low risk. `atlas.knowledge.node.know` is medium risk because it makes one internet-enabled Codex call, writes the selected Knowledge node, and may add immediate name-only unassessed children. It cannot rename, move, delete, merge, or recursively expand nodes.

Atlas functions are offered to ProductManager whenever Atlas is running and unlocked. Secret-store availability and a saved passphrase do not gate them. `Know_node` additionally requires a compatible Codex CLI. Atlas has an empty manifest resource scope; GitHub continues to use exact repository scopes.

## `Know_node` Context Boundary

Eidolon authorizes the caller and fetches one bounded node before Codex runs. An already-known node fails immediately. Codex receives only target identity/status/revision, canonical path, parent summary, immediate child names/statuses, the expansion rules, and the optional caller explanation. It never receives credentials, other Atlas record categories, unrelated Knowledge nodes, or the function catalog.

Live search is restricted to the public topic. A supplied explanation is authoritative, preserved after surrounding-whitespace normalization, and never searched, evaluated, or rewritten; Codex generates only terms and immediate child names. Without one, Codex also generates a direct explanation. Strict structured output is validated before Eidolon rechecks the observed revision, patches the target through the primitive Knowledge API, and creates missing immediate children through primitive create calls. Codex failure or invalid output performs no write. A stale revision detected before the patch performs no write and is not silently retried.

The primitive Atlas API makes the target patch and each child creation individually atomic; it does not provide a transaction spanning the full `Know_node` sequence. A later child-creation failure can therefore leave the target known and any earlier children created. Eidolon reports that failure and does not retry silently. This is the deliberate consequence of keeping Atlas primitive rather than adding an orchestration-specific endpoint.

Integration audit records contain caller/version attribution, operation, selected node ID when applicable, status, normalized error type, timestamps, and bounded sizes. They never contain personal content, explanations, terms, children, prompts, raw responses, exceptions, or secrets.
