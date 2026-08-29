# Google Calendar OAuth Integration

Eidolon supports one trusted Google Calendar connection and exactly five typed event functions. Every provider request targets the authenticated user's `primary` calendar. Google remains the event store and calendar UI; Eidolon has no event database, secondary-calendar selection, synchronization, cache, webhook, schedule, or calendar screen.

## OAuth Setup and Storage

In Google Cloud, enable the Google Calendar API and create an OAuth client of type **Web application**. Register this redirect URI exactly:

```text
http://localhost:8000/settings/integrations/google-calendar/oauth/callback
```

Settings accepts the OAuth client ID and client secret as write-only password inputs. Authorization uses Google's web-server authorization-code flow with offline access, consent prompting, a cryptographically random single-use state held in backend memory for ten minutes, and only these scopes:

```text
openid
email
https://www.googleapis.com/auth/calendar.events.owned
```

The callback consumes the state, rejects missing or denied Calendar permission, requires a refresh token, and retrieves a stable Google subject plus verified email through UserInfo. Eidolon stores one JSON bundle containing client ID, client secret, and refresh token in Windows Credential Manager under the `google_calendar` namespace. SQLite stores only the opaque secret reference, `credential_kind="oauth_refresh"`, verified email, a SHA-256-derived account identifier, sanitized status, and timestamps. Access tokens are refreshed for individual function calls and are never persisted.

A failed or abandoned replacement leaves the current connection active. A successful replacement removes the prior local secret; if the Google account identity changes, existing Google Calendar skill authorizations are invalidated. Disconnect removes only Eidolon's local credential and connection. It does not call Google's project-wide token-revocation endpoint.

OAuth callbacks always redirect to `/settings/integrations` with `connected`, `denied`, or `failed`; provider details are not returned. Uvicorn access logging removes the callback query string before formatting so authorization codes and state do not enter access logs.

## Function Contracts

The catalog exposes only:

| Function | Risk | Contract |
| --- | --- | --- |
| `google_calendar.event.create` | medium write | Requires non-empty `title`, `start`, and `end`; accepts nullable `description`/`location` and 1-20 recurrence lines; returns one normalized event. |
| `google_calendar.event.list` | low read | Accepts optional RFC3339 `time_min`/`time_max`, query, page size 1-100, and page token. Defaults `time_min` to invocation time and returns expanded upcoming default-event instances, effective bounds, and continuation state. |
| `google_calendar.event.get` | low read | Requires one exact event or series-master `id`; returns the normalized event or `not_found`. |
| `google_calendar.event.update` | medium write | Requires exact `id` and at least one mutable field. `title` maps to Google's summary. Start/end must be supplied together. An empty recurrence array clears recurrence. |
| `google_calendar.event.delete` | medium write | Requires exact `id`; deletes only that occurrence or series master and returns `{id, deleted: true}`. |

Inputs cannot provide calendar IDs, URLs, methods, headers, credentials, authentication, or notification controls. Trusted requests fix `/calendar/v3/calendars/primary/events`, reject redirects, bound time and response size, and suppress attendee update notifications. List fixes `singleEvents=true`, `orderBy=startTime`, `showDeleted=false`, and `eventTypes=default`. A continuation call must supply the returned `time_min`; callers also reuse the returned nullable `time_max`.

`EventTime` is exactly one form:

```json
{"date": "2026-09-01"}
```

or:

```json
{"date_time": "2026-09-01T09:00:00-04:00", "time_zone": "America/Toronto"}
```

Start and end use the same form. Timed values include an RFC3339 offset and the same valid IANA zone. End is exclusive and later than start. Recurrence accepts at most 20 bounded `RRULE`, `RDATE`, `EXDATE`, or `EXRULE` lines; `DTSTART` and `DTEND` are rejected because dedicated fields own those values.

Normalized events contain `id`, `status`, `summary`, nullable description/location, start/end, recurrence, `recurring_event_id`, `original_start`, Google Calendar link, and created/updated timestamps. List/Get expose instance and master identifiers without choosing an implicit update scope. Updating or deleting an occurrence ID affects only that instance; using `recurring_event_id` targets the master. “This and following” splitting is not supported.

## Trust Boundary

Generated server code calls the existing trusted integration helper with an exact selected operation. It never receives OAuth credentials, access tokens, provider URLs, authorization headers, callback routes, or a general Google client. The active manifest must declare `provider="google_calendar"`, exact operation IDs, and empty caller-selected resource scope. Connection availability does not grant authorization; the existing `integration_access` review remains required.

The capability scanner blocks direct Google API/OAuth/UserInfo hosts, Google-sensitive environment variables, authentication construction, secret-store access, Settings routes, undeclared operations, and browser integration calls. No-network Docker function and web-app runtimes resolve Google API and OAuth hosts to loopback; only the private trusted relay remains reachable. Provider failures are mapped to bounded integration error types, and audit resources contain only exact event IDs—never titles, descriptions, locations, times, tokens, requests, responses, or provider details.

## Trusted Settings API

- `GET /settings/integrations/google-calendar`: sanitized connection status and exact redirect URI.
- `POST /settings/integrations/google-calendar/oauth/start`: accepts write-only `client_id` and `client_secret`, creates pending state, and returns Google's authorization URL.
- `GET /settings/integrations/google-calendar/oauth/callback`: hidden OAuth callback that redirects with a bounded result.
- `DELETE /settings/integrations/google-calendar`: removes the local credential and connection.

OAuth setup never enters the function catalog. There are no public per-event routes; installed functions and Codex MCP use the existing typed integration invocation path after manifest, approval, connection, schema, and containment checks.
