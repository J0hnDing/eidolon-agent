# Act mode

Act is Eidolon's persistent local action agent. Every durable Act session owns a resumable Codex App Server thread. All sessions share this backend-managed structure:

    runtime/act/
      AGENTS.md
      memory/                 reserved and required to remain empty in v1
      workspace/              the only writable Codex root
        downloads/

Eidolon rewrites the managed AGENTS.md when its policy changes and refuses to start Act if memory contains files. Codex starts and resumes with runtime/act/workspace as its workspace-write root; the managed instructions and reserved memory directory are outside that writable root. Act receives no credential path or unrestricted host filesystem permission.

## App Server and MCP

Act owns a dedicated persistent Codex App Server process. This isolates long Act turns and MCP reloads from Product Manager sessions and account-usage reads. Before starting or resuming a thread, the backend verifies the Eidolon-owned MCP registration, reloads MCP configuration, and confirms that the running process exposes Eidolon tools. The Act process enables live web search and makes the Eidolon MCP tools callable without an additional headless Codex confirmation; backend function availability, integration authorization, and per-call invocation approval remain authoritative.

Every persisted thread is resumed with the current workspace, sandbox, developer instructions, model, and reasoning-effort route. A thread created in the current App Server process is used directly for its first turn, before a rollout exists. If Codex reports that an older rollout is missing before a new turn receives a live Codex turn id, Eidolon creates one replacement thread and sends up to 20 completed user/assistant exchanges as explicitly inert historical context. It does not replay old tool calls, failed turns, or any turn that crossed the live-turn boundary.

## Durable turns

POST /act/sessions/{sessionId}/turns persists a queued turn and returns 202 Accepted. A lifespan-owned dispatcher claims turns one at a time, marks them running, resumes the saved thread, records the live Codex turn id, and persists the final response and a concise activity list. Act conversations use the same `/chat` interface and local conversation list as Chat and Project; every row is marked with its fixed mode. The UI polls only while the selected Act conversation has a queued or running turn.

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
