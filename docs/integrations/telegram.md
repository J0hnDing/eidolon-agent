# Telegram Notification and Approval Bot

Eidolon supports four independent Telegram bots: `notification_approval`, `act_agent`, `observer_agent`, and `assistant_agent`. Settings presents each connection inside one Telegram section. Every token is write-only and stored only in the operating-system secret store.

Each agent bot accepts only its own paired private chat and has one selected session for its matching agent. `/new` creates a session, `/sessions` lists that agent's active sessions, and `/use <id>` selects one. An agent bot cannot select a session belonging to another agent. Ordinary text is durably queued and acknowledged immediately; its complete result is delivered later in Telegram-safe chunks. Assistant session creation shares the backend-wide five-session retention limit with UI conversations and scheduled assessments. If retention removes the selected Assistant session, the bot asks for `/new` or `/use` before accepting more conversation text.

Settings exposes `GET /settings/integrations/telegram`, `POST /settings/integrations/telegram/pairing/start`, `POST /settings/integrations/telegram/pairing/refresh`, and `DELETE /settings/integrations/telegram`. Pairing validates the token with `getMe`, rejects configured webhooks, returns a ten-minute one-time code, stores only its hash, and accepts only a matching `/start <code>` from a private chat. The paired chat and user are checked again for every callback.

While a valid code is pending, Settings reports `Awaiting private chat` until both the private chat and user IDs are bound. If the code expires before Telegram delivers the matching `/start` message, Settings reports `Pairing expired`; submitting the bot token again generates a fresh one-time code.

Four lifespan-owned workers use `getUpdates` long polling: one for notification/approval and one for each conversational agent. Each worker owns its database session, update offset, transient-error backoff, and polling request, so one disconnected or failing bot does not starve the others. Approval callback buttons carry only a record id, decision, kind, and opaque nonce; only the nonce hash is stored. Replayed, wrong-origin, and stale callbacks do not execute an action. Approval text is HTML-escaped and split below Telegram's 4,096-character limit; callback data remains below 64 bytes.

The worker refreshes the current persisted bot and pairing generation before handling each update. This prevents an in-flight 30-second request from comparing a newly generated code against stale pairing state or advancing a replacement bot's update offset.

Telegram also returns HTTP 409 when another `getUpdates` request briefly overlaps, such as during a backend restart. Eidolon treats that as a transient polling conflict rather than reporting a webhook. Only `getWebhookInfo` with a configured URL produces `webhook_conflict`, and a later clean poll restores a previously misclassified connection status.

`telegram.notification.send` accepts a title of 1-120 characters, a description of 1-800 characters, an optional HTTP(S) link of at most 2,048 characters, and an optional `alert` boolean that defaults to `false`. Notifications render as a bell followed by the bold title; alerts render with a red exclamation mark instead. The complete description follows the title. It is a medium-risk trusted integration write and does not itself require per-call approval. Internal approval delivery uses the same provider adapter as backend control-plane behavior, not as a catalog invocation.

The Assistant bot delivers special Assistant plan approvals and handles their callbacks. Normal function approvals remain on the notification bot. Proposal previews include the rationale, actions, references, and exact Act instruction. Pending proposals previously sent through the notification bot are resent through the Assistant bot with fresh nonces; old buttons cannot approve them. Pending delivery and execution-outcome updates use the Assistant bot's poller and pairing lifecycle.

Disconnecting either bot preserves unresolved local approvals. Pairing a replacement notification bot resends normal invocation approvals; pairing the Assistant bot resends special plan approvals. Disconnecting any agent bot removes only its selected-session binding; sessions and shared workspace files remain local.

Approval previews intentionally send complete bounded inputs to Telegram. The backend owns their presentation and stores the selected presentation snapshot with the approval. `email.send` uses a dedicated readable recipient, subject, body, and caller-supplied reason layout; other actions use readable parsed fields rather than raw JSON. After approval or denial, the status message is edited in place, its buttons are removed, and terminal execution failures include a sanitized code and explanation. Users should treat Telegram as a cloud privacy boundary.

Provider reference: [Telegram Bot API](https://core.telegram.org/bots/api).
