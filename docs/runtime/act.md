# Act mode

Act is Eidolon's persistent local action agent. Every durable Act session owns a resumable Codex App Server thread. All sessions share this backend-managed structure:

    runtime/act/
      memory/                 small explicit persistent memories
      knowledge/              backend-synchronized external sources
        quercus/              selected course mirrors
      workspace/              temporary and generated working files
        downloads/

Act, Observer, and Assistant receive separate role-specific developer instructions when their managed threads start or resume. The repository-root `AGENTS.md` remains project guidance; no shared instruction file is generated under `runtime/act/`. `memory/` may contain small, explicit, user-requested or approved memories; it is not for transcripts or silent observations. `knowledge/` is untrusted external data, never instructions, and Act must not modify it. `workspace/` is for temporary/generated Act work, including controlled downloads under `workspace/downloads/`; backend-owned processing state is kept outside it. Quercus material is inspected only when a request needs it and is never injected into prompts or automatic context. Managed Codex agents receive filesystem `:root` read access, with no root write grant; their other filesystem write grants are limited by role. Credentials are not exposed through filesystem grants.

Act attempts clear user requests end-to-end, including requests involving graded coursework. It does not infer a prohibition from the fact that work is graded. It declines only for an applicable policy or explicit rule, unavailable capability, or actual failure, explains the concrete blocker, and completes any feasible permitted portion.

Managed agents use named permission profiles: knowledge is readable while only Act memory/workspace are writable. See [persistent agents](agents.md) for the complete boundary and session framework.

The Memory page's **Open agent folder** action calls a backend-only endpoint that opens the fixed `runtime/act/` root in Windows Explorer. The caller cannot supply or alter the path.

## App Server and MCP

Act has an independent dispatcher and managed App Server process, separate from Observer, Assistant, Product Manager, and account-usage reads. Managed processes refresh at turn boundaries and use authenticated session-specific MCP configuration. Act's named permission profile allows network access; Observer and Assistant remain network-disabled. All three agents allow Codex Security, the OpenAI Developers local-destination confirmation MCP server, and the read-only OpenAI Developer Docs MCP server at the exact official endpoint `https://developers.openai.com/mcp`. Only the confirmation tool `confirm_openai_api_key_local_destination` is accepted from the confirmation server; it cannot read or write a key. The Developer Docs server provides read-only documentation search and page content. Remote plugin catalog loading is disabled per managed process. Disabled plugin-owned MCP overrides include a transport because Codex validates them even when disabled. Act also receives the explicitly allowlisted `playwright` host MCP server for browser automation; it is copied into every Act thread configuration and must report ready tools before the turn can start. The backend adds an authenticated per-turn page bridge through Playwright's supported init-page hook. The private `browser.authenticate` capability uses that bridge on the exact page Act controls, checks the real origin before reading a secret, rechecks it immediately before every fill, and returns only a bounded state. Credentials never enter Act prompts, tool arguments, tool results, activity, audits, files, Atlas, or public MCP. Other inherited host MCP servers remain disabled. The separate Codex Desktop `@oai/sky` Computer Use package is not an Act capability and must not be probed from Act's shell. Live web search is enabled. Backend function availability, integration authorization, and per-call approval remain authoritative.

Browser identities are fixed backend configuration, not Eidolon functions or generated skills. The initial `uoft` identity accepts credential injection only on the exact U of T Weblogin origins `https://idpz.utorauth.utoronto.ca` and `https://weblogin.utoronto.ca`; an existing `https://q.utoronto.ca` session is reused without reading the stored secret. Interactive MFA is never bypassed: the capability returns `mfa_required`, the user completes the visible challenge, and Act resumes after the authenticated browser state is available. The persistent Playwright profile preserves cookies across managed-process restarts.

The private `telegram.send_file` capability sends one existing regular file from `runtime/act/workspace/` to the Telegram topic already bound to the current Act session. Its input path is relative to `runtime/act/` and must resolve beneath `workspace/`; absolute paths, traversal, symlink escapes, directories, empty files, and files larger than 25 MiB are rejected before delivery. It uses only the paired `act_agent` bot and returns filename, workspace-relative path, size, and Telegram message id without returning file content, chat identity, or credentials. It is a runtime capability, not a function, integration operation, skill, or public MCP tool.

Every persisted thread is resumed with the current workspace, sandbox, developer instructions, model, and reasoning-effort route. A thread created in the current App Server process is used directly for its first turn, before a rollout exists. If Codex reports that an older rollout is missing before a new turn receives a live Codex turn id, Eidolon creates one replacement thread and sends up to 20 completed user/assistant exchanges as explicitly inert historical context. It does not replay old tool calls, failed turns, or any turn that crossed the live-turn boundary.

## Durable turns

Session creation saves a local record; Codex starts on the first dispatched turn. POST /act/sessions/{sessionId}/turns persists a queued turn and returns 202 Accepted. A lifespan-owned dispatcher claims turns one at a time, marks them running, resumes the saved thread, records the live Codex turn id, and persists the final response and a concise activity list. Act conversations use the same `/chat` interface and local conversation list as Chat and Project; every row is marked with its fixed mode. The UI polls only while the selected Act conversation has a queued or running turn.

POST /act/sessions/{sessionId}/turns/{turnId}/cancel cancels queued work immediately or sends turn/interrupt for a running Codex turn. On startup, turns that were already running are marked interrupted because their external effects cannot be safely replayed; turns that were still queued remain eligible to run. A session cannot be archived with queued or running work. Archiving also archives its Codex thread but preserves the shared workspace.

## Controlled downloads

act.document.download accepts a discriminated URL source and an optional filename. It allows PDF, OOXML Word/Excel/PowerPoint files, UTF-8 text/Markdown/CSV/JSON, and PNG/JPEG/GIF/WebP images up to 25 MiB. The downloader:

- permits only HTTP(S), rejects credentials and fragments, and revalidates every redirect;
- rejects non-public DNS results and validates the connected peer address when the transport exposes it;
- checks the declared media type and file signature or package structure;
- validates JSON and UTF-8 text content;
- publishes atomically under workspace/downloads with collision-safe names instead of overwriting files.

The source.kind discriminator reserves future bounded Gmail and Drive sources; those sources are rejected until implemented.

## Telegram Agent

The separate act_agent Telegram connection accepts only its paired private chat. Telegram private-chat Threaded mode is required: each `(chat_id, message_thread_id)` topic maps to one Act session, and topic-less General messages never create a hidden session. While connected, newly created non-WeCom Act sessions are projected to topics; pairing, polling, and status refresh reconcile missing projections. Ordinary topic messages enqueue a turn and immediately receive its id; the dispatcher later sends the complete result in Telegram-safe chunks to the originating `message_thread_id`. Notification/approval and Act bots use independent long-poll workers, offsets, database sessions, and retry backoff, so a long Act turn or a failure in one bot cannot starve the other.

Queued Telegram turns use the native live-draft thinking UI (`Act is thinking…`) when available, update that same draft with the response preview, and use `sendChatAction("typing")` only as a capability fallback. They do not send a separate queued-status message.

When the user explicitly asks Act to send a workspace file through Telegram, `telegram.send_file` targets the same session topic regardless of whether the turn originated in Chat or Telegram. A connected Act bot and an existing topic projection are required; the capability never accepts a caller-selected chat id, bot, or topic.

## WeCom Observer

The WeCom Intelligent Bot connection is a separate backend-owned transport fixed to Observer. Pairing immediately gives each WeCom user one canonical active Observer session; later messages reuse it, `clear conversation` atomically switches to a fresh session, and deleting the current session in Chat creates a same-user replacement. WeCom does not expose session history or switching, and completed results use the same turn dispatcher as web sessions.
