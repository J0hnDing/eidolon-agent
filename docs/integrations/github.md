# GitHub Integration Capability

Eidolon supports one trusted, secrets-backed integration provider: GitHub. Installed skills can request a fixed set of read-only GitHub operations, but they never receive the credential, an authorization header, a secret-store reference, a provider URL, or a generic authenticated HTTP client.

The authoritative operation contract is the typed registry in `backend/app/services/integration_registry.py`. Adjacent documentation links here instead of reproducing that contract.

For the distinction between adding a GitHub operation and adding a different provider, see [Extending the function catalog](../runtime/function_extension_guide.md).

## Trust Boundary

The React Settings page is the only user-facing credential-management surface. Its routes are trusted settings routes and are deliberately absent from the unified function catalog. Generated skills, runtime containers, browser code, agents, Codex prompts, and test adapters cannot create, inspect, enumerate, validate, replace, remove, or reveal credentials.

Production storage uses Windows Credential Manager through the narrow `SecretStore` interface. Eidolon's database stores only the provider, secret-store implementation id, opaque reference, sanitized status, validated account identity, and lifecycle timestamps. There is no environment-variable, configuration-file, SQLite-secret, generated-file, or application-encryption fallback. If the operating-system store is unavailable, locked, unsupported, or fails, connection management and invocation fail closed.

Adding or replacing a token validates it through GitHub's authenticated-user endpoint before a new secret becomes active. A failed validation or secret-store write leaves the previous connection unchanged. After a successful database switch, the replaced operating-system entry is removed. If the validated GitHub account id changes, existing GitHub skill authorizations are invalidated.

Removing the connection removes the operating-system credential and active connection metadata. Historical sanitized approvals and invocation audits remain, but future calls are blocked.

## Authoritative Operations

All operations are GET-only, low risk, side-effect free, bounded by per-operation timeouts and response sizes, and configured to reject redirects. REST requests are authenticated by backend-created headers, limited to `https://api.github.com`, and pinned to GitHub REST API version `2022-11-28`. The Trending operation additionally makes one unauthenticated request to the fixed `https://github.com/trending` page; it cannot navigate elsewhere on `github.com`. The registry owns input and normalized output JSON Schemas, scope behavior, provider request construction, pagination/result limits, error behavior, audit resource fields, fake behavior, and usage examples.

| Operation | Scope | Main bounds and normalized result |
| --- | --- | --- |
| `github.repository.get` | exact repository | One normalized repository metadata object; 1 MB provider-response limit. |
| `github.repository.tree.list` | exact repository | Recursive provider tree filtered to requested path and depth 1-5; at most 500 sorted entries; 5 MB provider-response limit; explicit `truncated`. |
| `github.repository.file.read` | exact repository | One UTF-8 text file; at most 262,144 decoded bytes; binary, non-UTF-8, directory, and oversized content are rejected. |
| `github.issue.list` | exact repository | One bounded provider page and at most 100 normalized issues; pull requests returned by GitHub's issues endpoint are removed; explicit `truncated`. |
| `github.pull_request.list` | exact repository | One bounded provider page and at most 100 normalized pull requests; explicit `truncated`. |
| `github.repository.trending.list` | none | At most 25 repositories in GitHub Trending order, each with a bounded README under the contract below. |

Callers never provide HTTP methods, URLs, paths, headers, authentication, GraphQL, or free-form query construction. Provider objects are projected into registry-defined normalized fields before output validation and return.

### Trending Contract

`github.repository.trending.list` reads GitHub's server-rendered Trending page rather than inventing a ranking from repository search:

- `period` is `daily`, `weekly`, or `monthly` and defaults to `weekly`;
- an optional literal programming-language path may be supplied;
- maximum results is 25 and GitHub's displayed order is preserved as `rank`;
- each result contains only Trending-relevant fields: repository identity and URL, description, language, total stars, total forks, and stars gained during the selected period;
- after selecting the requested results, the backend calls GitHub's preferred-README REST endpoint and returns at most 12,000 UTF-8 bytes as `readme` with explicit `readme_truncated`;
- an oversized README is truncated explicitly; a missing or unsupported README produces `readme: null` without changing GitHub's ranking;
- an empty or structurally incompatible Trending page fails as `provider_unavailable`; there is no silent repository-search fallback;
- output reports the `github_trending` ranking id, period, applied language, normalized repositories, and whether the parsed page contained more entries than the requested limit.

Tests use deterministic fake results and never depend on live GitHub ordering.

## Manifest Contract

The active manifest declares authorization intent:

```json
{
  "integration_requirements": [
    {
      "provider": "github",
      "operations": [
        "github.repository.get",
        "github.repository.file.read"
      ],
      "resource_scope": {
        "repositories": [
          "owner/repository"
        ]
      }
    }
  ]
}
```

Only `github` is supported. Operations must exist in the registry and be unique. Repository scope is normalized to lowercase exact `owner/repository` values; duplicates, wildcards, organization-wide scope, account-wide scope, and path wildcards are invalid. Repository-scoped operations require at least one applicable repository. The non-repository trending operation requires an empty repository scope unless it is combined with repository-scoped operations in the same provider requirement.

The declaration cannot contain credential material, headers, secret references, provider tokens, or capability tokens. The backend derives and persists the active normalized integration contract from the actual active manifest.

## Connection Versus Skill Authorization

A connected GitHub account does not authorize any skill. Runtime review creates a separate `integration_access` approval showing provider, selected operations, read-only status, normalized repositories, and current connection availability. Approval never happens automatically.

The backend fingerprints:

- provider;
- sorted selected operation ids;
- sorted normalized repository scope;
- each selected registry operation's contract version.

Authorization is tied to the caller skill and exact contract fingerprint. An unchanged fingerprint can be reused by a later version. Operation or scope expansion and registry contract change create a distinct decision without breaking the still-active version's unchanged authorization. A validated account identity change invalidates all existing GitHub authorizations. Removing a credential blocks calls without erasing historical approvals.

## Runtime Invocation

Function implementation code uses:

```python
integration_runtime_capabilities.call(
    operation="github.repository.get",
    input={"owner": "owner", "repository": "repository"},
)
```

Web-application server code uses `web_runtime_capabilities.call_integration`. Browser code cannot invoke integrations.

The helpers use the existing ephemeral function-run or web-application instance capability. No-network Docker runtimes reach only the exact integration invocation relay path over the private runtime network; the backend performs the GitHub request. This does not grant direct GitHub connectivity, arbitrary internet access, general backend API access, or settings access.

After relay authentication establishes the caller, every invocation rechecks:

1. caller identity and active version;
2. installed and enabled state;
3. function or web-application runtime eligibility;
4. actual active manifest validity and ordinary runtime approval;
5. declared integration requirement;
6. current contract authorization;
7. GitHub connection availability;
8. operation registry membership;
9. normalized repository scope;
10. input JSON Schema.

Only then does trusted provider execution retrieve the credential. Caller-supplied skill ids, versions, provider identity, repository authorization, URLs, paths, and headers are never trusted. Nested provider/capability calls are unsupported.

## Agent and Test Context

GitHub operations are integration-category entries in the unified function catalog. ProductManager receives only available entry ids, titles, concise descriptions, categories, and risks and selects exact ids in the blueprint. For Task DAG builds it assigns selected ids through each node's `function_ids`. Builder receives registry-derived schemas, examples, scope rules, helper guidance, and fake-test guidance only for that node's selected functions. The single-Codex workflow receives full context for every function selected in the blueprint. Update and repair prompts preserve the same selected-only rule.

Tester receives the selected operation context plus `integration_test_adapter.DeterministicFakeIntegrationAdapter`. Generated tests require no token and make no live GitHub requests.

## Static Validation

The deterministic capability scanner rejects generated implementation that:

- reads token, credential, secret, or GitHub-sensitive environment variables;
- imports operating-system credential-store APIs;
- constructs authorization or bearer headers;
- directly contacts GitHub API hosts, even when ordinary GitHub network permission is declared;
- addresses trusted settings routes or the internal integration HTTP path;
- bypasses the stable helper, dynamically constructs operation ids, or calls undeclared/unselected operations;
- places integration calls or capability material in browser assets;
- serializes capability or credential material.

This scanner is a mismatch detector. The relay, runtime caller identity, active manifest, authorization, scope, schema, trusted provider adapter, and sandbox remain the authoritative security boundary.

## Errors and Auditing

Generated code receives only bounded normalized errors: `connection_unavailable`, `invalid_credential`, `operation_undeclared`, `authorization_missing_or_stale`, `repository_outside_scope`, `invalid_input`, `not_found`, `provider_forbidden`, `rate_limited`, `provider_timeout`, `response_too_large`, `unsupported_file_type`, `provider_unavailable`, or `internal_failure`.

Once a caller identity and current version are established, each allowed or denied attempt gets one sanitized `integration_audit_records` row with caller skill/version, function run or web-app instance, operation id, normalized repository when applicable, timestamps, status, normalized error type, and bounded JSON size metadata. Audits never contain tokens, headers, secret references, capability values or hashes, raw authenticated requests, provider headers, raw responses, or provider exception text. Audit persistence fails closed.

## Trusted API Surface

- `GET /settings/integrations/github`: sanitized connection status.
- `PUT /settings/integrations/github`: validate and add or replace the token; the token is write-only.
- `DELETE /settings/integrations/github`: remove the active operating-system credential and connection.
- `POST /integrations/capabilities/invoke`: hidden function-run capability path.
- `POST /web-apps/capabilities/integrations/invoke`: hidden web-application instance capability path.

There is no public route per GitHub operation and no runtime discovery route.
