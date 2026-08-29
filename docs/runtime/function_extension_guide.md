# Extending the Function Catalog

Catalog registration describes a callable contract. It does not implement the callable. Every platform function needs both trusted runtime behavior and matching catalog metadata.

## User Function

```text
ProductManager blueprint owns object input/output schemas
  -> Builder implements runtime=function package
  -> Tester and backend validate package
  -> User approves runtime permissions and installs
  -> Backend creates Skill and active SkillVersion
  -> Catalog refresh derives the user-function entry and availability
```

No separate registration code is required.

When Codex MCP registration is enabled, an available installed function is also discovered automatically the next time the MCP process starts. Its input/output schema and description come from the same catalog entry; no MCP-specific function registration is required.

## New Operation for an Existing Integration Provider

Example: add another GitHub read operation.

```text
Add typed operation contract to integration_registry.py
  -> Implement GitHub request and normalized output in github_provider.py
  -> Add deterministic fake behavior
  -> Verify manifest declaration, scope, approval fingerprint, and audit behavior
  -> Add schema, authorization, provider-error, and secret-leak tests
  -> Catalog refresh registers the operation automatically
```

The provider owns its API, authentication, request construction, response normalization, limits, and provider-specific errors. The common integration service owns caller validation, manifest declarations, approval, credential retrieval timing, schema validation, and sanitized auditing.

## New Integration Provider

Example: add a provider other than GitHub.

```text
Define the provider's operation contracts
  -> Implement a provider-specific credential or OAuth validator and adapter
  -> Add trusted credential lifecycle and connection availability
  -> Route common IntegrationService checks to that provider adapter
  -> Add provider-specific runtime and fake-test behavior
  -> Add authorization, scope, audit, failure, and secret-leak tests
  -> Catalog refresh publishes available operations
```

Keep provider transport and normalization in its own adapter. Shared integration enforcement remains in `IntegrationService`; do not add provider-specific URLs, credentials, or response handling to runtime helpers.

OAuth setup routes are trusted Settings controls, not functions. For example, Google Calendar authorization start/callback/disconnect lives under `/settings/integrations/google-calendar`, while only the five typed `google_calendar.event.*` operations enter the registry and catalog. An OAuth provider must keep pending state bounded and single-use, store only its long-lived credential in the operating-system secret store, refresh short-lived access on demand, and exclude setup, token, and generic HTTP operations from generated code.

Available typed integration operations are likewise included automatically in new MCP process snapshots. Preserve accurate `read_only`, side-effect, provider boundary, limits, and contract-version metadata because those fields drive MCP annotations and stale-contract checks.

## Backend-Core Function

```text
Design input/output and permission contract
  -> Implement trusted backend service and capability route
  -> Implement function/web-app runtime helper and relay access as needed
  -> Add permission enforcement and scanner bypass protection
  -> Add catalog metadata to function_catalog_seed.json
  -> Add helper, authorization, schema, relay, and negative tests
  -> Catalog refresh publishes the backend-core function
```

Adding only a seed entry is invalid: ProductManager could select a function that has no callable implementation.
