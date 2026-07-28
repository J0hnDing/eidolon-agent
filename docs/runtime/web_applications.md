# Sandboxed Web Application Runtime

`runtime = web_app` is a persistent execution protocol for self-rendered application skills. It is separate from `runtime = function`, which remains the bounded JSON stdin/stdout protocol. A web application package declares an importable ASGI entrypoint such as `app:app`; it does not add source to the Eidolon React frontend.

## Ownership Boundary

The skill owns its HTML, CSS, JavaScript, application routes, interaction, in-memory state, and domain logic. Package assets stay under the active immutable version folder.

The platform owns installation and approval, version activation, Docker or explicit local-development startup, the trusted ASGI host, readiness, loopback ingress, the private ingress/capability relay, resource limits, cache mounting, network mode, session origins, proxying, security headers, scoped privileged capabilities, lifecycle records, logs, shutdown, and stale-instance recovery.

## Runtime Flow

1. The user opens an installed, enabled web application with approved runtime permissions.
2. The backend reuses a healthy instance pinned to the active version or starts one while holding only the short `web_app_start` operation lock.
3. The trusted runtime host imports the manifest entrypoint and exposes the platform-owned readiness path `/__personal_agent__/health`.
4. After bounded readiness succeeds, the backend creates a distinct user/application session and returns an opaque `*.web-app.localhost` embedding URL.
5. The Applications UI keeps navigation, identity, version, status, permission information, logs, and stop controls in trusted React chrome and embeds only the returned origin in a sandboxed iframe.

An idle application is not represented by a long-running `SkillRun`, and it does not retain a skill operation lock. `SkillRun` remains the bounded execution/audit model for function skills.

## Origin and Browser Containment

Each session receives a unique origin under `*.web-app.localhost` on the backend port. The session bearer is returned once as part of that hostname; SQLite stores only its hash and a non-secret origin identity. Requests on one of these hosts are rewritten to the internal gateway route before normal FastAPI routing, so an untrusted origin cannot address `/skills`, `/memory-facts`, or another trusted backend route. The internal gateway path returns `404` on ordinary backend origins.

The iframe sandbox is:

```text
allow-scripts allow-forms allow-same-origin allow-modals
```

`allow-same-origin` lets one isolated application origin use its own JavaScript state and same-origin routes. It does not make the frame same-origin with the React parent. `allow-modals` permits application confirmation dialogs. The sandbox still omits top navigation, popups, downloads, pointer lock, presentation, and storage-access escape flags. The iframe grants no privileged browser feature through `allow` and sends no referrer.

Every gateway response replaces upstream privilege-bearing headers and applies a platform policy that includes:

- `default-src 'self'` with explicit same-origin script, style, connect, form, worker, image, font, and media rules;
- `base-uri 'none'`, `object-src 'none'`, restricted `frame-ancestors`, and `form-action 'self'`;
- a deny-by-default `Permissions-Policy` for camera, microphone, geolocation, display capture, clipboard, payment, USB, and related features;
- `Referrer-Policy: no-referrer`, `Cross-Origin-Resource-Policy: same-origin`, `X-Content-Type-Options: nosniff`, and `Cache-Control: no-store`.

The gateway strips upstream cookies and redirects, forwards only a small header allowlist, applies request/response size limits, and rejects parent-traversing paths. WebSockets are intentionally unsupported in Milestone 1. Browser-side external traffic is blocked entirely rather than mapped to server permissions: the CSP permits same-origin application routes only, and the static capability scan blocks literal absolute HTTP(S) URLs in HTML, CSS, and JavaScript assets. The static scan is a mismatch detector, not a permission inference mechanism.

## Ingress, Egress, and Capabilities

Application ingress and internet egress are independent:

- Every Docker web application starts on a private internal network. A separate trusted relay container attaches to that network and Docker bridge, publishes the ephemeral loopback ingress port, and exposes back to the application only the exact backend Codex, Function, and integration capability routes. The relay has no skill package mount or instance token; arbitrary Eidolon API paths are not proxied.
- Without approved manifest network domains, the untrusted application container remains only on that internal network and has no internet route.
- With approved domains, the application container receives Docker bridge as a secondary egress network while ingress and Eidolon capabilities still use the relay. Common Docker Desktop host aliases are suppressed inside the untrusted container. The manifest and runtime approval name explicit domains, but domain-level filtering and direct-IP/private-network enforcement are not implemented; this limitation is shown in the application containment details and tracked in TODO-010.
- The explicit `local` or `dev` runner mode is marked `local_dev`. It binds only loopback but cannot provide Docker filesystem, process, resource, or network isolation.

Generated browser code never receives an Eidolon API credential. Approved server-side Codex access uses the trusted `web_runtime_capabilities.call_codex` helper. Approved GitHub integration access uses `web_runtime_capabilities.call_integration`; the helper supplies only a literal registry operation and schema-validated input, never a token, URL, or header. Both helpers read the instance capability from the controlled server process environment. The backend authenticates its hash, rechecks the enabled skill and active version, and enforces the applicable active-manifest and approval contract. Caller-supplied skill ids are not trusted. Integration behavior is detailed in [GitHub integration capability](../integrations/github.md).

## State and Filesystem

In-memory application state is ephemeral across stop, failure, update activation, backend restart, or idle shutdown. Persistent skill-owned state is limited to the existing per-skill `./cache` root:

- Docker mounts the active package read-only and mounts the controlled cache at `/skill/cache` read/write.
- The local-development fallback uses the controlled cache parent as its working directory while importing immutable source through `PYTHONPATH`.
- The runtime sets `PERSONAL_AGENT_SKILL_CACHE_DIR` to the controlled cache. Generated code must use that value (or `./cache` as a development fallback), never `__file__/cache` or another package-relative writable path.
- Package code should resolve read-only HTML/CSS/JavaScript assets relative to `__file__`, not the process working directory.

Web applications do not receive direct database access, arbitrary filesystem paths, secrets, shell access, custom Dockerfiles, or custom startup commands.

## Persistent Records and Statuses

`web_app_instances` stores version-pinned service state with `starting`, `ready`, `healthy`, `unhealthy`, `stopped`, or `failed` status plus application/relay runner identity, loopback upstream, readiness/access timestamps, bounded logs, and diagnostics.

`web_app_sessions` stores active, closed, or expired application sessions separately from the shared instance. Multiple sessions may reuse one healthy version-pinned instance while retaining distinct browser origins and session ids.

`web_app_audit_records` stores bounded startup, readiness, stop, gateway-request, session-open, and privileged-operation metadata. Records are capped per instance. Application request/response bodies and Codex prompt/response content are not stored there.

## Lifecycle Policy

| Event | Running-instance behavior |
| --- | --- |
| Open | Lazy start or reuse the healthy active-version instance; wait for readiness before returning an embedding URL. |
| Idle timeout | Stop the instance and close active sessions. The default timeout is 15 minutes. |
| Disable | Acquire a short per-skill disable lock, stop active instances, close sessions, then persist `enabled = false`. |
| Update or repair draft | Leave the immutable active version and its instance unchanged while the draft is built and tested. |
| Activate another version | Stop old-version instances before switching the active pointer. A later open starts the new version. |
| Delete | Stop runtime resources, remove sessions/audit/instance rows, then remove skill lifecycle data and files. |
| Backend shutdown | Stop every active Docker container or local-development process and close sessions. |
| Backend startup | Treat rows left in active states as stale, attempt cleanup, and persist a deterministic stopped/failed result. |
| Readiness or gateway failure | Persist failed or unhealthy state with bounded diagnostics; do not return or continue a healthy session. |

Configuration uses the existing `PERSONAL_AGENT_RUNNER_MODE` plus `PERSONAL_AGENT_WEB_APP_*` settings for readiness, idle/session timeouts, maintenance cadence, runtime root, gateway domain, internal Docker network, and the approved-egress DNS resolver (`PERSONAL_AGENT_WEB_APP_EGRESS_DNS`, default `1.1.1.1`). If the gateway domain is customized, the frontend must use the matching `VITE_WEB_APP_GATEWAY_DOMAIN` value.

## API Surface

- `POST /web-apps/{skill_id}/sessions`: authorize, lazy-start/reuse, and return the controlled embedding session.
- `GET /web-apps/{skill_id}/instances`: list lifecycle diagnostics.
- `GET /web-apps/{skill_id}/audit`: list bounded interaction and privileged-operation records.
- `POST /web-apps/{skill_id}/stop`: explicitly stop the application.
- `POST /web-apps/capabilities/codex`: instance-capability-authenticated server-side Codex access.
- `POST /web-apps/capabilities/integrations/invoke`: hidden instance-capability-authenticated registry integration access.

The host-routed proxy endpoint is internal and omitted from OpenAPI. A web application's server-side code may invoke manifest-declared functions through `web_runtime_capabilities.call_function` or approved GitHub operations through `call_integration`; the backend rechecks the instance and complete target contract. This does not create a hybrid runtime and the instance capability is never exposed to browser code. Public hosting, remote multi-user access, arbitrary function or integration selection, browser automation, nested capability calls, and WebSockets are outside this runtime contract.
