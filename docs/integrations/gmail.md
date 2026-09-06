# Gmail OAuth Integration

Eidolon supports one trusted Gmail connection and five provider-neutral `email.*` functions. Gmail and Google Calendar share one Google OAuth application client, but each service has its own authorization grant, refresh token, scopes, account identity, connect/disconnect controls, and authorization records. The two services may therefore be connected to different Google accounts, and replacing one account does not change the other.

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
| `email.search` | Requires at least one keyword/filter. Returns one page of at most 25 normalized conversation summaries and an opaque continuation token. |
| `email.conversation.get` | Fetches one Gmail thread in full format and returns at most 100 normalized messages. HTML-only bodies are converted to text. Attachments expose only filename, MIME type, and size. |
| `email.read_new` | Fetches at most 50 unread Primary Inbox messages newer than one year without changing their read state. It excludes Promotions, Social, Updates, Forums, Spam, and Trash. Every selected message must be fetched and the combined normalized result must fit 4 MiB. |
| `email.read_and_mark_new` | Fetches the same bounded unread Primary Inbox batch, then removes the `UNREAD` label from exactly the messages successfully fetched after the combined normalized result fits 4 MiB. |
| `email.send` | High risk. Sends one plain-text email to 1-10 direct recipients and at most 20 total recipients. Subject and body are bounded, and HTML and attachments are not accepted. Every invocation requires the separate per-call approval workflow. |

Normalized messages contain message/conversation IDs, bounded sender and recipient headers, subject, date, snippet, text, unread state, and attachment metadata. Eidolon never returns raw MIME, attachment bytes, OAuth responses, authorization headers, or provider credentials.

`email.send` returns `{sent, message_id, conversation_id}` only after an approved execution. Calling its effective public contract first creates a pending invocation approval; it does not contact Gmail or retrieve the Gmail credential before approval.

## Trust and Privacy Boundary

Generated skill code can request only exact declared `email.*` operations through Eidolon's trusted integration relay. It cannot choose provider URLs, HTTP methods, headers, credentials, Gmail query syntax, label mutations, MIME payloads, or attachment downloads. Provider responses and normalized outputs are bounded. Audit records contain only operation identifiers and bounded status/resource identifiers, never message content, recipients, OAuth data, or tokens.

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
