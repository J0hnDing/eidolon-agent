# Gmail OAuth Integration

Eidolon supports one default trusted Gmail connection and five provider-neutral `email.*` functions. Gmail and Google Calendar share one Google OAuth application client, but each service has its own authorization grant, refresh token, scopes, account identity, connect/disconnect controls, and authorization records. The two services may therefore be connected to different Google accounts, and replacing one account does not change the other. Email provider choice is made by the invocation input; Gmail is one adapter behind the shared contract rather than a separate public operation family.

## OAuth Setup and Storage

In Google Cloud, enable the Gmail and Google Calendar APIs and create one OAuth client of type **Web application**. Configure it once in Eidolon's shared Google connection section and register both service callback URIs. Gmail uses this one:

```text
http://localhost:8000/settings/integrations/gmail/oauth/callback
```

Authorization uses Google's web-server flow with offline access, `prompt=consent select_account`, and a cryptographically random single-use state held in backend memory for ten minutes. Eidolon requests only:

```text
openid
email
https://www.googleapis.com/auth/gmail.modify
```

Google classifies `gmail.modify` as a restricted scope. A Google Cloud project used outside its configured test users may therefore require OAuth consent-screen verification and the additional controls required by Google.

The callback requires the Gmail scope and a refresh token, then retrieves the verified Google account identity. Eidolon stores the shared client ID and secret once in the OS secret store under `google_oauth`, while Gmail's grant stores only its refresh token under `gmail`. SQLite receives only opaque secret references, verified account identifier/email, and sanitized status. Access tokens are assembled from the shared client and Gmail grant only for provider calls and are never persisted or returned.

## Function Contracts

| Function | Behavior |
| --- | --- |
| `email.search` | Requires unique `providers: ["gmail"|"outlook"]` and at least one keyword/filter. `page_size` is 1–25 per provider; the merged page can contain up to 50 conversations and uses a versioned composite opaque token. |
| `email.conversation.get` | Requires `provider: "gmail"` or `"outlook"`; fetches one Gmail thread in full format and returns at most 100 normalized messages. HTML-only bodies are converted to plain text, messages are chronological, and attachments expose only filename, MIME type, and size. |
| `email.read_new` | Requires `providers`. Fetches at most 50 unread Primary Inbox messages newer than one year without changing their read state. It excludes Promotions, Social, Updates, Forums, Spam, and Trash. Every selected message must be fetched and the per-provider normalized result must fit 4 MiB. |
| `email.read_and_mark_new` | Requires `providers`. Fetches the same bounded unread Primary Inbox batch, then removes the `UNREAD` label only after fetch validation. It returns provider-level exact marked/failed/unknown message IDs and bounded provider errors. |
| `email.send` | Requires one `provider` and is high risk. Sends one plain-text email to 1-10 direct recipients and at most 20 total recipients. Subject and body are bounded, and HTML and attachments are not accepted. Every invocation requires the separate per-call approval workflow. |

Normalized messages contain `provider`, message/conversation IDs, bounded sender and recipient headers, subject, UTC RFC 3339 `timestamp`, snippet, plain text, unread state, and attachment metadata. Conversation summaries contain `provider`, conversation id, subject, latest sender/timestamp, snippet, message count, unread, and attachment state. Search and unread results are globally newest-first; conversation messages are chronological. Eidolon never returns raw MIME, attachment bytes, OAuth responses, authorization headers, or provider credentials.

`email.send` returns `{provider, sent, message_id, conversation_id}` only after an approved execution. Calling its effective public contract first creates a pending invocation approval; it does not contact Gmail or retrieve the Gmail credential before approval. Changing the selected provider, default connection, account, or contract makes an approval stale.

## Trust and Privacy Boundary

Generated skill code can request only exact declared provider-neutral `email.*` operations through Eidolon's trusted integration relay. It cannot choose provider URLs, HTTP methods, headers, credentials, Gmail query syntax, label mutations, MIME payloads, or attachment downloads. Provider responses are bounded to 4 MiB per provider and 8 MiB for a two-provider aggregate. Audit records contain operation, provider, internal connection/account attribution, and bounded status/resource identifiers, never message content, recipients, OAuth data, or tokens.

For approved email sends, the user-selected Telegram approval channel intentionally receives the reason, recipients, subject, and complete bounded plain-text body. This sends that approval content to Telegram's cloud service before Gmail execution. Disconnecting Telegram after an approval was queued leaves local approval available, while changing the connected Gmail account makes the queued action stale.

## Trusted Settings API

- `GET /settings/integrations/gmail`: sanitized connection status and exact redirect URI.
- `GET /settings/integrations/google`: sanitized shared OAuth-client status and both redirect URIs.
- `PUT /settings/integrations/google/oauth-client`: stores the shared write-only client ID and client secret.
- `DELETE /settings/integrations/google/oauth-client`: removes the shared client only when Calendar and Gmail are disconnected.
- `POST /settings/integrations/gmail/oauth/start`: starts a Gmail-specific grant using the configured shared client and returns the authorization URL.
- `GET /settings/integrations/gmail/oauth/callback`: hidden OAuth callback that redirects with a bounded result.
- `DELETE /settings/integrations/gmail`: removes only Eidolon's local Gmail credential and connection.

Gmail setup is not a callable integration function. Persistent `integration_access` authorization and, for `email.send`, a fresh per-call invocation approval remain separate requirements.

Provider references: [Google OAuth web-server flow](https://developers.google.com/identity/protocols/oauth2/web-server), [Gmail OAuth scopes](https://developers.google.com/workspace/gmail/api/auth/scopes), [Gmail threads.get](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.threads/get), and [Gmail messages API](https://developers.google.com/workspace/gmail/api/reference/rest).
