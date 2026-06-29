# Codex CLI Integration

The backend can use the local Codex CLI to evaluate Project mode requests and generate proposed application skills.

## Required Local Setup

Install and log in to the Codex CLI on the host machine:

```powershell
codex login
codex doctor
```

By default, the backend uses `PERSONAL_AGENT_CODEX_MODE=auto`. In auto mode it uses the real Codex CLI when `codex` is available on `PATH`; otherwise it falls back to the fake/dev adapter.

These defaults are already built in:

```text
PERSONAL_AGENT_CODEX_MODE=auto
PERSONAL_AGENT_CODEX_COMMAND=codex
PERSONAL_AGENT_CODEX_SANDBOX=workspace-write
PERSONAL_AGENT_CODEX_PLAUSIBILITY_SANDBOX=read-only
PERSONAL_AGENT_CODEX_CHAT_SANDBOX=read-only
PERSONAL_AGENT_CODEX_APPROVAL_POLICY=never
PERSONAL_AGENT_CODEX_ENABLE_SEARCH=auto
```

Only set environment variables when you want to override the defaults:

```powershell
$env:PERSONAL_AGENT_CODEX_COMMAND = "codex"
$env:PERSONAL_AGENT_CODEX_SANDBOX = "workspace-write"
$env:PERSONAL_AGENT_CODEX_PLAUSIBILITY_SANDBOX = "read-only"
$env:PERSONAL_AGENT_CODEX_CHAT_SANDBOX = "read-only"
$env:PERSONAL_AGENT_CODEX_APPROVAL_POLICY = "never"
$env:PERSONAL_AGENT_CODEX_ENABLE_SEARCH = "auto"
```

To force fake/dev mode:

```powershell
$env:PERSONAL_AGENT_CODEX_MODE = "fake"
```

To force real mode even if auto-detection would fail:

```powershell
$env:PERSONAL_AGENT_CODEX_MODE = "real"
```

Optional:

```powershell
$env:PERSONAL_AGENT_CODEX_MODEL = "gpt-5"
$env:PERSONAL_AGENT_CODEX_TIMEOUT_SECONDS = "300"
$env:PERSONAL_AGENT_CODEX_PLAUSIBILITY_TIMEOUT_SECONDS = "120"
$env:PERSONAL_AGENT_CODEX_CHAT_TIMEOUT_SECONDS = "120"
```

## Permission Boundary

Generation uses:

```text
codex --ask-for-approval never exec -C skills/proposed/<skill_name> --sandbox workspace-write --skip-git-repo-check -
```

This gives Codex a writable root inside the proposed skill folder. It should not modify backend, frontend, app tests, Git metadata, or installed skills.

Plausibility review uses:

```text
codex --ask-for-approval never exec -C <project_root> --sandbox read-only -
```

This lets Codex evaluate whether the user's Project mode request is plausible without writing files.

Chat mode uses:

```text
codex --ask-for-approval never exec -C <project_root> --sandbox read-only -
```

This lets Codex answer normal chat messages without creating or modifying application skills.

## Web Search

`PERSONAL_AGENT_CODEX_ENABLE_SEARCH=auto` enables Codex CLI web search during generation only when the approved generation plan includes requested network domains or package dependencies.

This is build-time research access only. It does not grant generated skills runtime network access. Runtime network execution remains blocked until the app can enforce domain-level network sandboxing.

## Do Not Use

Do not use these for this project integration:

```text
--dangerously-bypass-approvals-and-sandbox
--sandbox danger-full-access
```

The app's security model depends on Codex writing only proposed skill files and on the app separately validating, approving, installing, and running skills.
