# Codex CLI Integration

This is a short compatibility page for the original Codex CLI setup notes. The main integration documentation now lives in:

- [Codex CLI Integration](integrations/codex_cli.md)
- [Agent Workflow](agents/overview.md)
- [Project Build Workflow](workflows/project_build_workflow.md)

## Defaults

The backend can use the local Codex CLI for:

- direct Chat-mode answers;
- Project-mode plausibility and ProductManager work;
- Builder skill generation and repair;
- Tester test authoring;
- update review and summaries.

Default environment values:

```text
PERSONAL_AGENT_CODEX_MODE=auto
PERSONAL_AGENT_CODEX_COMMAND=codex
PERSONAL_AGENT_CODEX_SANDBOX=workspace-write
PERSONAL_AGENT_CODEX_PLAUSIBILITY_SANDBOX=read-only
PERSONAL_AGENT_CODEX_CHAT_SANDBOX=read-only
PERSONAL_AGENT_CODEX_APPROVAL_POLICY=never
PERSONAL_AGENT_CODEX_ENABLE_SEARCH=auto
```

`auto` uses real Codex when `codex` is available on `PATH`; otherwise the app uses fake/dev adapters.

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

## Boundaries

Chat, plausibility, and ProductManager workflow actions run read-only. ProductManager returns structured JSON and the backend writes workflow artifact files. Builder/Tester skill build, repair, test-authoring, and update work runs with `workspace-write` scoped to the controlled skill or draft-version directory. Codex must not modify backend or frontend app source while generating an application skill.

Build-time web search may be enabled when approved permissions request runtime `network` entries or package dependencies. Runtime network access is separate and still requires manifest declaration, runtime approval, and runner support.

Do not use:

```text
--dangerously-bypass-approvals-and-sandbox
--sandbox danger-full-access
```
