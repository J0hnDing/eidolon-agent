# WhatsApp through Baileys

Eidolon links one WhatsApp account as a companion device using a backend-owned
Baileys process. It exposes five trusted integration functions. Incoming messages
update a local cache; they do not enqueue Assistant, Act, or Observer turns.

## Setup

Install Node.js 24 or later, then install the checked-in bridge dependencies once:

```powershell
cd C:\Users\John\Projects\Eidolon\bridges\whatsapp
npm ci
```

Restart the backend after installing or changing integration code. Open
**Settings → Integrations → WhatsApp**, choose **Pair WhatsApp**, and scan the QR
code from WhatsApp's **Settings → Linked Devices → Link a Device**. Pairing and
credential controls are trusted Settings actions, never catalog functions.
Existing MCP processes need restarting to discover the new catalog snapshot.

Baileys is an independent WhatsApp Web client, not the official Business API.
Its pinned version is `@whiskeysockets/baileys@7.0.0-rc14`. WhatsApp controls what
history a linked device receives; synchronization completion does not establish
that the cache contains every conversation or message.

## Functions

| Function | Behavior |
| --- | --- |
| `whatsapp.contact.search` | Optional name/phone query over cached contacts. |
| `whatsapp.chat.list` | Optional query and `direct`/`group` filter over known chats. |
| `whatsapp.message.list` | Requires one exact `chat_id`; returns recent cached messages newest first. |
| `whatsapp.message.search` | Requires text `query`; optionally filters by `chat_id`, `since`, and `until`. |
| `whatsapp.message.send` | Requires exact `recipient` and 1–4,000 characters of `text`; optional `reply_to_message_id` must identify a retained text message in that same chat. |

Reads use `page_size` (default 50, maximum 100) and opaque `cursor` pagination.
Results contain `items`, `next_cursor`, and `coverage`: retention cutoff, oldest
cached message, last synchronization time, and synchronization state. They never
mark messages read. Functions require an online paired connection; offline status
and recovery controls remain available in Settings.

Recipients are an international phone number beginning with `+`, or a direct,
LID, or group chat identifier returned by the integration. Broadcasts, channels,
statuses, arbitrary API requests, group administration, media downloads, and
outbound attachments are outside this contract.

Reads are low risk. Sends declare the `send` effect and high risk, so every send
uses Eidolon's existing standing authorization and per-call approval workflow.
The approval includes the exact recipient, complete text, optional quote ID, and
caller-provided `reason_to_call`. The existing Telegram notification/approval bot
must be paired to submit a new approval; approval content is consequently sent to
Telegram. No WhatsApp send or phone-number resolution happens before approval.
Observer remains read-only.

Approved execution rechecks the connection/account and uses a durable internal
approval identity to prevent duplicate dispatch. A successful result reports
`accepted`, `message_id`, and `chat_id`; it does not promise delivery or reading.
A timeout or interrupted send is `outcome_unknown` and is never automatically
retried. Check WhatsApp before deciding to send a new message.

## Storage and lifecycle

FastAPI starts the optional bridge for pairing or a saved session, supervises
crashed children, and stops its child on shutdown. Communication is bounded JSON
over private stdin/stdout, without a bridge HTTP listener or caller-selected
commands. Missing dependencies, unavailable credentials, and provider failures
do not prevent Eidolon startup. Socket reconnect uses bounded backoff; logout or
an invalid session requires explicit pairing again.
Saved linked-device authentication is recognized by its Web identity, including
sessions whose mobile-registration flag remains false. Recovery publishes
connection availability changes to the function catalog automatically.

STDIO MCP processes do not own a second bridge. WhatsApp tool calls use a fixed
loopback backend endpoint, which authenticates the agent credential again and
runs the ordinary invocation kernel against the backend's existing connection.
Public MCP calls require the local Codex-tools connection to remain enabled.
The endpoint rejects browser origins, remote callers, other providers, and
caller-supplied identity fields. Invalid agent credentials never fall back to
public-user authority. No provider credentials or approval bypass are forwarded.

The database lives in the ignored `runtime/whatsapp/<connection-generation>/`
directory. Authentication credentials and Signal keys are encrypted with
AES-256-GCM. The encryption key lives only in Windows Credential Manager and is
passed to the child through private stdin. SQLite connection metadata stores an
opaque credential reference. There is no plaintext credential fallback.

Cached message text and contact/chat metadata are local SQLite data, separate
from Eidolon's editable memory. Messages older than 30 days are rejected and
pruned, with periodic cleanup while the bridge runs and immediate pruning on
startup/read. Edits, deletions, identity aliases, and known disappearing-message
expiry update the cache. View-once content and media bytes are excluded.

**Clear cached history** deletes cached messages, chats, and contacts while
preserving pairing. Historical replay is suppressed after clearing; future
received messages can accumulate again. **Disconnect** revokes local authority,
attempts remote logout, and deletes local authentication and cached data. A failed
remote logout is disclosed so the user can remove the linked device on the phone.
Failed local credential/file cleanup stays revoked and can be retried; it blocks
new pairing until cleanup succeeds.

Operational logs and integration audits exclude message text, QR values, and
credentials. Existing approval records intentionally retain their bounded
approved input, preview, and outcome under the existing approval contract.

References: [Baileys](https://github.com/WhiskeySockets/Baileys),
[QR pairing](https://github.com/WhiskeySockets/docs/blob/main/authentication/qr-code.mdx),
and [session storage](https://github.com/WhiskeySockets/docs/blob/main/authentication/session-management.mdx).
