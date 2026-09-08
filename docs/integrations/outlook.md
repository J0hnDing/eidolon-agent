# Outlook OAuth and Microsoft Graph Integration

Outlook is the Microsoft adapter for Eidolon's five shared `email.*` operations. It is selected at invocation time with `provider: "outlook"` for conversation/send operations or with an `providers` array for search/unread operations. Settings exposes one default Outlook connection; the database may retain non-default rows for migration and future account management, but there is no public account selector.

## OAuth and storage

Configure a Microsoft Entra application as a web client with this redirect URI:

```text
http://localhost:8000/settings/integrations/outlook/oauth/callback
```

Eidolon uses the `common` authority so personal, work, and school accounts can participate:

```text
https://login.microsoftonline.com/common/oauth2/v2.0/authorize
```

The authorization-code flow uses backend-held, expiring single-use state, PKCE with `S256`, `prompt=select_account`, and these scopes:

```text
openid profile email offline_access User.Read Mail.ReadWrite Mail.Send
```

The client secret is stored in the OS secret store. SQLite stores only the sanitized client id, authority, secret-store implementation id, and opaque secret reference. The refresh token is stored in the OS secret store under the Outlook namespace. The Graph subject is converted to a salted-by-namespace SHA-256 account identity before persistence; the account email is retained only for the sanitized Settings status display. Access tokens, authorization headers, and token responses are never persisted or returned. A school tenant may still require administrator consent according to tenant policy.

Trusted Settings routes are:

- `GET /settings/integrations/microsoft`: sanitized client configuration status, common authority, and redirect URI.
- `PUT /settings/integrations/microsoft/oauth-client`: write-only client id/secret replacement; an active Outlook connection must be disconnected first.
- `DELETE /settings/integrations/microsoft/oauth-client`: removes the client secret/configuration after Outlook disconnect.
- `GET /settings/integrations/outlook`: sanitized default-account status.
- `POST /settings/integrations/outlook/oauth/start`: creates state/PKCE and returns the consent URL.
- `GET /settings/integrations/outlook/oauth/callback`: consumes the state, exchanges the code, verifies `/me`, and redirects with only a bounded result marker.
- `DELETE /settings/integrations/outlook`: removes the local default connection and refresh-token reference.

## Microsoft Graph adapter

The adapter uses Microsoft Graph v1.0 and sends `Prefer: IdType="ImmutableId", outlook.body-content-type="text"` on mail requests. It never follows a caller-provided URL. Search calls `/me/messages` with bounded `$top` and safely escaped `$search` expressions, groups returned messages by `conversationId`, and normalizes each summary. Date, unread, and attachment constraints use bounded Graph filters. Conversation reads fetch at most 100 messages and sort normalized messages chronologically. Provider responses and normalized results stay within the shared 4 MiB per-provider budget.

Outlook “new” means unread messages in the Inbox, classified as Focused, received within the previous year, capped at 50. The full result is normalized and budget-checked before marking. Marking sends Graph JSON batches of at most 20 `PATCH isRead=true` subrequests, inspects every subresponse, and rereads ambiguous message states. The shared result reports exact marked, failed, and unknown message ids/counts. Remaining failures or unknown states emit `partial_mutation`; a timeout that cannot be reconciled leaves the affected ids in `unknown_message_ids`.

Send creates a plain-text draft at `/me/messages` first, records its immutable message and conversation ids, then sends that draft through `/send`. A failed or timed-out send never claims success and reports that an unsent draft or unknown delivery state may remain. OAuth, Graph, throttling, transport, and provider messages are mapped to bounded safe errors without raw Graph bodies or sensitive headers.

Graph continuation values are reduced to an opaque `$skiptoken` only when Microsoft returns the exact expected `https://graph.microsoft.com/v1.0/me/messages` continuation endpoint. Public search pagination wraps one provider cursor per selected provider in a versioned composite token whose provider set must match the next request. Embedded URLs, alternate hosts, paths, and oversized/control-character cursors are rejected.

## Trusted skill boundary and acceptance

Generated skills can invoke only the trusted provider-neutral email functions. Static scanning and Docker host blocking reject direct Graph and Microsoft OAuth host access, just as they reject direct Gmail access. Outlook provider adapters own URLs, headers, queries, batches, credentials, and normalization; generated code receives no token or transport capability.

Automated Graph tests use fake transports for OAuth authority/PKCE/scopes, query conversion, normalization, Focused unread selection, pagination, batch partial failures/reconciliation, immutable draft/send, throttling, and safe errors. Final manual acceptance still requires a personal Microsoft account and a school/work account to verify consent policy, search/read, marking, and send behavior.

References: [Microsoft authorization-code flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow), [message collection `$search`](https://learn.microsoft.com/en-us/graph/search-query-parameter), [immutable Outlook message ids](https://learn.microsoft.com/en-us/graph/outlook-immutable-id), [create a message/draft](https://learn.microsoft.com/en-us/graph/api/user-post-messages?view=graph-rest-1.0), and [send mail](https://learn.microsoft.com/en-us/graph/api/user-sendmail?view=graph-rest-1.0).
