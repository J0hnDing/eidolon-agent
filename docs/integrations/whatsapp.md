# WhatsApp through Whatsmeow

Eidolon links one WhatsApp account as a companion device using a backend-owned
Go/Whatsmeow process. It exposes the five general WhatsApp functions and one
Assistant-only Muse conversation function. Incoming messages update a local cache.
When Assistant access to Muse is enabled, replies resume existing exchanges and
new live messages initiated by Muse create an Assistant agenda conversation.
Other incoming WhatsApp messages do not enqueue agent work.

## Setup

Install Go 1.27.3 or later, then build the pinned bridge dependencies once:

```powershell
cd C:\Users\John\Projects\Eidolon\bridges\whatsapp
.\build.ps1
```

Restart the backend after installing or changing integration code. Open
**Settings → Integrations → WhatsApp**, choose **Pair WhatsApp**, and scan the QR
code from WhatsApp's **Settings → Linked Devices → Link a Device**. Pairing and
credential controls are trusted Settings actions, never catalog functions.
Existing MCP processes need restarting to discover the new catalog snapshot.

Live validation on 2026-10-25 confirmed new QR pairing, restoration after bridge
restart, and reading an incoming Muse rich-text reply through MCP. An outbound
self-chat test was submitted through Eidolon's per-call approval flow; live send
acceptance remains unverified until that approval is completed. Earlier cached
`unsupported` entries retain `text: null` unless WhatsApp supplies them again;
the bridge does not reconstruct missing bodies or claim complete phone history.

The script also accepts `-GoPath` and recognizes an optional portable Go installation
at `runtime/toolchains/go/bin/go.exe`. The executable is written to ignored
`runtime/bin/eidolon-whatsapp-bridge.exe` (without `.exe` on other platforms).
Eidolon never builds code or downloads dependencies during startup. For development,
run `go test ./...` in the bridge directory. `go.mod` and `go.sum` pin the dependency
graph, including Whatsmeow commit `b2b1e71d45e2`.

Whatsmeow is an independent WhatsApp Web client, not the official Business API.
WhatsApp controls what
history a linked device receives; synchronization completion does not establish
that the cache contains every conversation or message.

## Functions

| Function | Behavior |
| --- | --- |
| `whatsapp.contact.search` | Optional name/phone query over cached contacts. |
| `whatsapp.chat.list` | Optional query and `direct`/`group`/`agent` filter over known chats. |
| `whatsapp.message.list` | Requires one exact `chat_id`; returns recent cached messages newest first. |
| `whatsapp.message.search` | Requires text `query`; optionally filters by `chat_id`, `since`, and `until`. |
| `whatsapp.message.send` | Requires exact `recipient` and 1–4,000 characters of `text`; optional `reply_to_message_id` must identify a retained text message in that same chat. |
| `whatsapp.muse.converse` | Assistant-only send/wait/resume with the fixed Muse recipient after one-time Settings permission. |

Reads use `page_size` (default 50, maximum 100) and opaque `cursor` pagination.
Results contain `items`, `next_cursor`, and `coverage`: retention cutoff, oldest
cached message, last synchronization time, and synchronization state. They never
mark messages read. Functions require an online paired connection; offline status
and recovery controls remain available in Settings.

Recipients are an international phone number beginning with `+`, or a direct,
LID, or group chat identifier returned by the integration. Broadcasts, channels,
statuses, arbitrary API requests, group administration, media downloads, and
outbound attachments are outside this contract.

Read functions also recognize Meta AI identifiers ending in `@bot`. They have
no phone-number projection and appear as `type: "agent"`. A provider directory
entry alone does not establish that a saved conversation exists or that its name
is Muse. Ordinary `whatsapp.message.send` continues to exclude these identifiers.
The expanded read contracts are version 2; refresh old MCP snapshots and review
affected installed-skill authorization rather than silently expanding old grants.

History synchronization is tracked from the supplied history batches and provider
events. A quiet or interrupted transfer becomes `partial`/`paused`, rather than
remaining `syncing` indefinitely. Completion of an offline-message transfer still
does not establish complete history coverage.

Trusted Settings exposes `GET /settings/integrations/whatsapp/diagnostics` for
cached item counts and bounded history-phase/error metadata, without bodies,
names, identifiers, or credentials. Explicit
`POST /settings/integrations/whatsapp/agents/discover` performs a bounded read-only
provider bot-directory lookup. It does not send a message or create a saved chat.
Trusted `POST /settings/integrations/whatsapp/history` accepts one cached `chat_id`
and asks the account's own phone for up to 50 earlier messages. Bot chats use an
exact request for their latest cached message instead. Provider acceptance does
not guarantee that the phone will supply history. These requests are excluded
from the function catalog and are blocked after Clear cached history; ordinary
read functions always read the existing cache. Recovery responses for bot chats
are accepted only from the linked account for an exact, recently requested ID.

Muse's observed native identity is `1807055946647697@bot`; the bridge supplies
the known name only after that chat is observed, without overwriting supplied
contact names or treating provider-directory membership as a chat. Whatsmeow owns
the Muse WASA protocol and persists its synchronized root secrets in the encrypted
authentication database. Incoming decoded text follows the same cache path as
normal chats. Bot-forwarded/task envelopes and known AI rich-response text
primitives are projected to cached text; media, HTML primitives, layout data and private
recipient metadata are excluded. Streamed edits replace the original cached
reply, and historical replay cannot revert retained edits or deletions.
Reading one's own phone-originated prompt copy does not establish
reply decryption: live validation must observe an incoming Muse reply. This
Muse conversation behavior is described below.

## Assistant conversations with Muse

Under **Settings → Integrations → WhatsApp → Assistant conversations with Muse**,
enable Assistant access and confirm the linked account. This grants Assistant
permission to send selected context and messages to Meta/Muse, respond when Muse
initiates a conversation, and follow up without per-message approval. Conversations
use flexible guidance of roughly 30 messages total across both sides, including
Muse's opening message. The Assistant follows ideas and feedback freely and closes
naturally around that length; this is not a fixed quota or a backend-enforced cap,
and there is no fifteen-minute conversation deadline. It grants no authority over other recipients.
Disable revokes it even while offline. Logout, account replacement, relinking and
Disconnect invalidate it; pairing alone never enables it.

`whatsapp.muse.converse` retains `HIGH` risk and `read`/`send` effects. Its canonical
standing-grant mode is checked by the normal provider-neutral authorization policy
and then revalidated by the adapter. Act, Observer, public-user MCP, installed
functions/services and web apps cannot use Assistant's grant. The backend supplies
the fixed Muse ID; the function accepts no recipient or caller-controlled authority.

For a new message, supply `text` (1–4,000 characters), a stable `request_id`
(8–64 letters/digits/underscores/hyphens), optional `purpose` and a retained
Muse `reply_to_message_id`. Reuse a request ID only for the identical action.
To resume, supply only its `exchange_id` and optional `wait_seconds` (0–20,
default 10). Send plus waiting stays within one bounded bridge request.

Results report `pending`, `replied`, `outcome_unknown`, `cancelled` or `expired`,
the accepted outgoing ID when known, normalized cached replies, and cache coverage.
Native ancestry correlates a reply with the original message. Shared encryption
root IDs and unrelated phone prompts cannot establish reply ownership. Streamed
edits update the retained reply before completion. Uncertain sends retain their
dispatch identity and resume reads only; they are never sent again automatically.

Muse also sends replies with only its shared encryption-root reference. When no
explicit parent is present, the bridge can associate a live reply with the sole
accepted pending exchange for that account: the outgoing prompt must still be
cached, the reply must follow it, and no other outgoing Muse prompt may have
intervened. Competing or uncertain exchanges, conflicting parent metadata and
revoked grants prevent this fallback. The encryption root itself is never used
as a prompt identifier. Reference-only live-event metadata permits a reply that
arrives before send acceptance to be reconciled afterward without resending;
historical cache messages alone cannot create an association.

A terminal stream marker makes the retained reply ready immediately. Unmarked
text settles after 1.5 seconds without an edit; `first`/`inner` fragments settle
after 10 seconds without an edit because Muse can omit its terminal marker even
for a complete rich-text answer. Every new edit restarts the quiet period. This
is a readiness heuristic, not proof that the provider will send no later edits.
The same rule applies to reply continuations and incoming conversation starts.

A pending or uncertain exchange owns one waiting Assistant agenda item, containing
only its reference, original task reference and bounded purpose. Maintenance checks
existing exchanges without sending new messages and makes that item due when a
result is ready. Normal Assistant activation preserves user-conversation priority.
Revocation or cache clearing cancels waiting work. Reply bodies remain solely in
the rolling cache; deletion, disappearing expiry and retention also apply to Muse.

The private `muse_incoming` bridge reader returns durable, ordered references to
new live text from the fixed Muse identity. It excludes supplied history, recovered
replays, cached duplicates, phone-originated prompts and replies already associated
with Assistant exchanges, including late replies to closed exchanges. Streamed edits
update the same reference and settle before activation; deleted, expired or cleared
messages cannot start a conversation. The reader is not a public function or MCP tool.

Maintenance consumes these references into ordinary due Assistant agenda work,
checking the current account, standing grant and Assistant function policy before
and after reading. `muse_inbox_states` keeps one cursor and active conversation
owner per grant; cursor advancement and agenda creation commit together, so restart
or concurrent polling cannot create a second conversation for the same message.
Only activity observed and sent after the grant was enabled is eligible. Revocation,
account changes and cache clearing dismiss outstanding incoming-conversation work.
Additional incoming activity reuses its open/waiting ledger rather than resetting
the approximate message count. Activation preserves user-work priority and requires
proactive Assistant to be enabled. The ledger contains provider references and
operating guidance; message bodies remain in the rolling cache and are read at
execution. The Assistant keeps the same ledger and roughly 30-message guidance
through delayed reply continuations, and treats Muse content as external input.

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
Recovery publishes connection availability changes to the function catalog
automatically. The bridge has no dependency on Node or Baileys.

### Migrating an existing Baileys connection

Baileys authentication is not imported into Whatsmeow. Existing installations enter
`relink_required` and invalidate standing authorizations and pending sends, even
if the same account is paired again. Open Settings and choose **Pair WhatsApp again**.
The old encrypted authentication records remain unused until Disconnect removes
the connection directory; the existing 30-day cache is retained when the same
account pairs. Pairing a different account clears the previous account's cache.
Remove the old Eidolon device from WhatsApp's Linked Devices after the new
connection is verified. Disconnect logs out the current Whatsmeow device; it
cannot remotely log out the old Baileys device.

STDIO MCP processes do not own a second bridge. WhatsApp tool calls use a fixed
loopback backend endpoint, which authenticates the agent credential again and
runs the ordinary invocation kernel against the backend's existing connection.
Public MCP calls require the local Codex-tools connection to remain enabled.
The endpoint rejects browser origins, remote callers, other providers, and
caller-supplied identity fields. Invalid agent credentials never fall back to
public-user authority. No provider credentials or approval bypass are forwarded.

The database lives in the ignored `runtime/whatsapp/<connection-generation>/`
directory. Whatsmeow's SQLite authentication store (`whatsmeow-auth.sqlite`) uses
the maintained `go-sqlite3` Adiantum encrypted VFS for credentials, Signal keys,
message secrets, and app-state records. Database and journal data are encrypted;
temporary storage stays in memory. Writes use SQLite transactions and full
synchronization before the provider store operation returns. Adiantum provides
encryption at rest, not authentication against malicious file modification.
The random 256-bit encryption key lives only in Windows Credential Manager and is
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

Startup upgrades older `muse_exchanges` tables with the nullable `sent_message_id`
column before WhatsApp recovery starts. Existing exchange identities, uncertain-send
state and continuation references are preserved; a missing outgoing ID remains
unknown rather than causing a resend. This also lets disconnect and cleanup retries
revoke Muse exchanges on databases created before that column was introduced.

Operational logs and integration audits exclude message text, QR values, and
credentials. Existing approval records intentionally retain their bounded
approved input, preview, and outcome under the existing approval contract.

References: [Whatsmeow](https://github.com/tulir/whatsmeow),
[SQLite session store](https://github.com/tulir/whatsmeow/tree/main/store/sqlstore),
and [Adiantum VFS](https://github.com/ncruces/go-sqlite3/tree/main/vfs/adiantum).
