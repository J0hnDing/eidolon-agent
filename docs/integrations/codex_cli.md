# Codex CLI Integration

The backend can use the local Codex CLI for direct chat, Project-mode plausibility review, skill planning, Builder edits, Tester test writing, repairs, updates, and ProductManager summaries.

## Defaults

```text
PERSONAL_AGENT_CODEX_MODE=auto
PERSONAL_AGENT_CODEX_COMMAND=codex
PERSONAL_AGENT_CODEX_SANDBOX=workspace-write
PERSONAL_AGENT_CODEX_PLAUSIBILITY_SANDBOX=read-only
PERSONAL_AGENT_CODEX_CHAT_SANDBOX=read-only
PERSONAL_AGENT_CODEX_APPROVAL_POLICY=never
PERSONAL_AGENT_CODEX_ENABLE_SEARCH=auto
```

`auto` uses real Codex when `codex` is on PATH, otherwise fake/dev adapters are used.

## Setup

```powershell
codex login
codex doctor
```

Optional overrides:

```powershell
$env:PERSONAL_AGENT_CODEX_MODE = "real"
$env:PERSONAL_AGENT_CODEX_MODEL = "gpt-5"
$env:PERSONAL_AGENT_CODEX_TIMEOUT_SECONDS = "300"
```

## Sandbox Modes

- Chat and plausibility: read-only project root.
- Skill generation/build/update: writable controlled skill directory.

Codex must not modify backend/frontend app source when generating application skills.

## Web Search

Build-time Codex search may be enabled when the approved plan requests network domains or package dependencies. This is for generation research only. It does not grant runtime access to generated skills.

## Prohibited CLI Options

Do not use:

```text
--dangerously-bypass-approvals-and-sandbox
--sandbox danger-full-access
```
