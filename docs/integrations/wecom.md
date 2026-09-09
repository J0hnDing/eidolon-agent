# WeCom Observer transport

WeCom is an Observer-only external chat transport. It is registered as a provider (`wecom`) with no integration operations, so it never appears in the function catalog, MCP discovery, skill manifests, or runtime integration authorization.

Eidolon uses the WeCom Intelligent Bot WebSocket long-connection mode at the fixed provider endpoint. Settings accepts a Bot ID and write-only Secret; the secret is stored only in the operating-system secret store. There is no public webhook, CorpID/AgentID callback, or WeCom callable integration adapter.

The lifespan-owned `WeComObserverWorker` runs in the trusted backend. It owns socket authentication, heartbeat, reconnect, inbound callback processing, and outbound replies. Messages are fixed to `agent_id="observer"` and enter `ActSessionService(agent_id="observer")` and the normal agent turn dispatcher. Normal group messages, non-text messages, and voice messages without WeCom-provided transcription are ignored; group pairing attempts receive a private-chat-only rejection. Inbound message IDs are persisted per connection before pairing, command, or turn handling to prevent duplicate turns.

The single Observer bot supports multiple paired users. Settings lists them and **Add user** opens an Eidolon modal containing a ten-minute one-time code. Only the code hash and expiry are persisted. A user pairs by sending `/pair <code>` from a private WeCom chat; unpaired users are ignored and pairing from groups is rejected. Each paired user has exactly one canonical `current_session_id`. The first ordinary message creates it; every later message uses it. WeCom does not expose session listing, switching, or history selection. `clear conversation` (or `/clear`) retires the current session without hard-deleting its history, creates a fresh session immediately, and atomically replaces the current pointer. Removing a user deletes only that binding; disconnecting removes the credential, all bindings, and message-deduplication records. Observer sessions and history are preserved in both cases.

If a migrated or externally archived current session is no longer active, the next ordinary message creates and persists one fresh current session for that user. This is a WeCom-only adapter rule; the core session service has no default-session repair or cross-channel fallback.

Settings exposes only the transport controls:

- `GET /settings/integrations/wecom`: sanitized Bot ID, connection status, pending pairing expiry, and paired users. Current session IDs are intentionally not exposed through WeCom settings.
- `PUT /settings/integrations/wecom`: stores a replacement Bot ID and write-only Secret, then returns the short-lived pairing code.
- `POST /settings/integrations/wecom/pairing/start`: invalidates any pending code and returns a new short-lived code for **Add user**.
- `DELETE /settings/integrations/wecom/users/{userId}`: removes only the selected user binding.
- `DELETE /settings/integrations/wecom`: removes the credential and binding without deleting Observer sessions.

Each queued turn snapshots the originating connection, WeCom user ID, and resolved session. Completed turns use the transport-neutral `deliver_agent_turn_result(...)` path and are delivered only while that session remains the user's current active session; clearing a conversation therefore cannot send an old-session result into the fresh conversation. Its persisted delivery provider selects Telegram or WeCom, while the agent dispatcher remains shared.

Protocol reference: [WeCom Intelligent Bot Node SDK](https://github.com/WecomTeam/aibot-node-sdk).
