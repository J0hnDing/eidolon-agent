# Telegram Notification and Approval Bot

Eidolon supports one active `notification_approval` Telegram bot and an independent `act_agent` Telegram bot. Settings presents them inside one Telegram section with separate **Notification/Approval bot token** and **Agent bot token** controls. Both tokens are write-only and stored only in the operating-system secret store.

The Act bot accepts only its paired private chat. It has one selected Act session at a time: `/new` creates one, `/sessions` lists active sessions, and `/use <id>` selects one. Ordinary text is durably queued and acknowledged immediately; its complete result is delivered later in Telegram-safe chunks.

Settings exposes `GET /settings/integrations/telegram`, `POST /settings/integrations/telegram/pairing/start`, `POST /settings/integrations/telegram/pairing/refresh`, and `DELETE /settings/integrations/telegram`. Pairing validates the token with `getMe`, rejects configured webhooks, returns a ten-minute one-time code, stores only its hash, and accepts only a matching `/start <code>` from a private chat. The paired chat and user are checked again for every callback.

While a valid code is pending, Settings reports `Awaiting private chat` until both the private chat and user IDs are bound. If the code expires before Telegram delivers the matching `/start` message, Settings reports `Pairing expired`; submitting the bot token again generates a fresh one-time code.

Two lifespan-owned workers use `getUpdates` long polling: one for notification/approval and one for Act. Each worker owns its database session, update offset, transient-error backoff, and polling request, so either bot can be disconnected or fail without starving the other. Approval callback buttons carry only an approval id, decision, and opaque nonce; only the nonce hash is stored. Replayed, wrong-origin, and stale callbacks do not execute an action. Approval text is HTML-escaped and split below Telegram's 4,096-character limit; callback data remains below 64 bytes.

The worker refreshes the current persisted bot and pairing generation before handling each update. This prevents an in-flight 30-second request from comparing a newly generated code against stale pairing state or advancing a replacement bot's update offset.

Telegram also returns HTTP 409 when another `getUpdates` request briefly overlaps, such as during a backend restart. Eidolon treats that as a transient polling conflict rather than reporting a webhook. Only `getWebhookInfo` with a configured URL produces `webhook_conflict`, and a later clean poll restores a previously misclassified connection status.

`telegram.notification.send` accepts a title of 1-120 characters, a description of 1-800 characters, an optional HTTP(S) link of at most 2,048 characters, and an optional `alert` boolean that defaults to `false`. Notifications render as a bell followed by the bold title; alerts render with a red exclamation mark instead. The complete description follows the title. It is a medium-risk trusted integration write and does not itself require per-call approval. Internal approval delivery uses the same provider adapter as backend control-plane behavior, not as a catalog invocation.

Disconnecting the notification bot preserves unresolved local invocation approvals. Pairing a replacement notification bot resends unresolved actions with fresh callback nonces. Disconnecting the Act bot removes only its selected-session binding; Act sessions and workspace files remain local.

Approval previews intentionally send complete bounded inputs to Telegram. The backend owns their presentation and stores the selected presentation snapshot with the approval. `email.send` uses a dedicated readable recipient, subject, body, and caller-supplied reason layout; other actions use readable parsed fields rather than raw JSON. After approval or denial, the status message is edited in place, its buttons are removed, and terminal execution failures include a sanitized code and explanation. Users should treat Telegram as a cloud privacy boundary.

Provider reference: [Telegram Bot API](https://core.telegram.org/bots/api).
