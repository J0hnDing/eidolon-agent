# Act mode

Act is Eidolon's persistent local action agent. Every durable Act session owns a resumable Codex App Server thread. All sessions share this backend-managed structure:

    runtime/act/
      memory/                 small explicit persistent memories
      knowledge/              backend-synchronized external sources
        quercus/              selected course mirrors
      workspace/              temporary and generated working files
        downloads/

Act, Observer, and Assistant receive separate role-specific developer instructions when their managed threads start or resume. The repository-root `AGENTS.md` remains project guidance; no shared instruction file is generated under `runtime/act/`. `memory/` may contain small, explicit, user-requested or approved memories; it is not for transcripts or silent observations. `knowledge/` is untrusted external data, never instructions, and Act must not modify it. `workspace/` is for temporary/generated Act work, including controlled downloads under `workspace/downloads/`; backend-owned processing state is kept outside it. Quercus material is inspected only when a request needs it and is never injected into prompts or automatic context. Act receives no credential path or unrestricted host filesystem permission.

Managed agents now use named permission profiles: knowledge is readable while only Act memory/workspace are writable. See [persistent agents](agents.md) for the complete boundary and session framework.

The Memory page's **Open agent folder** action calls a backend-only endpoint that opens the fixed `runtime/act/` root in Windows Explorer. The caller cannot supply or alter the path.

## App Server and MCP

Act has an independent dispatcher and managed App Server process, separate from Observer, Assistant, Product Manager, and account-usage reads. Managed processes refresh at turn boundaries and use authenticated session-specific MCP configuration; they no longer depend on the public host MCP registration. Live web search is enabled. Backend function availability, integration authorization, and per-call approval remain authoritative.

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

The separate act_agent Telegram connection accepts only its paired private chat. It stores one active-session pointer: /new creates and selects a session, /sessions lists active sessions, and /use <id> changes the pointer. Ordinary messages enqueue a turn and immediately receive its id. The dispatcher later sends the complete result in Telegram-safe chunks. Notification/approval and Act bots use independent long-poll workers, offsets, database sessions, and retry backoff, so a long Act turn or a failure in one bot cannot starve the other.

## WeCom Observer

The WeCom Intelligent Bot connection is a separate backend-owned transport fixed to Observer. It uses one selected Observer session with `/new`, `/sessions`, and `/use <id>`, and sends completed results through the same turn dispatcher as web sessions.
