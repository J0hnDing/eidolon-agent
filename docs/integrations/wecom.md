# WeCom Observer transport

WeCom is an Observer-only external chat transport. It is registered as a provider (`wecom`) with no integration operations, so it never appears in the function catalog, MCP discovery, skill manifests, or runtime integration authorization.

Eidolon uses the WeCom Intelligent Bot WebSocket long-connection mode at the fixed provider endpoint. Settings accepts a Bot ID and write-only Secret; the secret is stored only in the operating-system secret store. There is no public webhook, CorpID/AgentID callback, or WeCom callable integration adapter.

The lifespan-owned `WeComObserverWorker` runs in the trusted backend. It owns socket authentication, heartbeat, reconnect, inbound callback processing, and outbound replies. Messages are fixed to `agent_id="observer"` and enter `ActSessionService(agent_id="observer")` and the normal agent turn dispatcher. Normal group messages, non-text messages, and voice messages without WeCom-provided transcription are ignored; group pairing attempts receive a private-chat-only rejection. Inbound message IDs are persisted per connection before pairing, command, or turn handling to prevent duplicate turns.

The single Observer bot supports multiple paired users. Settings lists them and **Add user** opens an Eidolon modal containing a ten-minute one-time code. Only the code hash and expiry are persisted. A user pairs by sending `/pair <code>` from a private WeCom chat; unpaired users are ignored and pairing from groups is rejected. Each user binding has its own selected-session pointer. `/sessions` lists the shared canonical active Observer sessions, `/use <id>` selects any of them for that user, and `/new` creates a canonical Observer session selected only for that user. Removing a user deletes only that binding; disconnecting removes the credential, all bindings, and message-deduplication records. Observer sessions and history are preserved in both cases.

Before every normal message, the binding pointer is repaired through the shared agent transport fallback: use it when it names an active Observer session, otherwise select and persist the most recently updated active Observer session, or create and persist one when none exists. An archived pointer is therefore transparent to the WeCom user.

Settings exposes only the transport controls:

- `GET /settings/integrations/wecom`: sanitized Bot ID, connection status, pending pairing expiry, and paired users with their selected-session pointers.
- `PUT /settings/integrations/wecom`: stores a replacement Bot ID and write-only Secret, then returns the short-lived pairing code.
- `POST /settings/integrations/wecom/pairing/start`: invalidates any pending code and returns a new short-lived code for **Add user**.
- `DELETE /settings/integrations/wecom/users/{userId}`: removes only the selected user binding.
- `DELETE /settings/integrations/wecom`: removes the credential and binding without deleting Observer sessions.

Each queued turn snapshots the originating connection and WeCom user ID. Completed turns use the transport-neutral `deliver_agent_turn_result(...)` path, so a later `/use` does not redirect an in-flight reply. Its persisted delivery provider selects Telegram or WeCom, while the agent dispatcher remains shared.

Protocol reference: [WeCom Intelligent Bot Node SDK](https://github.com/WecomTeam/aibot-node-sdk).
