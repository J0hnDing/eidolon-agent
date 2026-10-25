<div align="center">

# Eidolon

### A local-first personal AI assistant with reusable skill management.

[![Status](https://img.shields.io/badge/status-active%20development-f59e0b?style=for-the-badge)](#project-status)
[![Local first](https://img.shields.io/badge/local--first-yes-14b8a6?style=for-the-badge)](#system-capabilities)
[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](backend/pyproject.toml)
[![React](https://img.shields.io/badge/React-18-61DAFB?style=for-the-badge&logo=react&logoColor=111827)](frontend/package.json)
[![License](https://img.shields.io/badge/license-Apache--2.0-6366f1?style=for-the-badge)](LICENSE)

</div>

## A personal assistant that gets to know you

Eidolon's Assistant has two main jobs: **understand you and help you**. It stays curious about your experiences, motivations, and interests, building understanding through conversation. With proactive mode enabled, it maintains an agenda to plan useful work, research what matters to you, and follow up after important events. It can return with a result, ask how something went, or offer emotional company, respecting your commitments and quiet hours. See [Assistant behavior](docs/runtime/assistant.md).

## Eidolon-Atlas

[Eidolon-Atlas](docs/integrations/atlas.md) is a separate local, encrypted store for your personal context: goals, projects, interests, experiences, relationships, beliefs, and knowledge. You can inspect, edit, and delete that information directly. Authorized agents and skills can read relevant records, and write-capable agents can update supported content as you share new information. This gives the assistant durable context for choosing useful help. Atlas remains the source of truth for those records.

## Reusable skills

Turn recurring needs into tested Python capabilities through **Skills → Build a skill**. Review proposals, code, and permissions; then manage installation, versions, updates, and removal. Skills can use approved integrations and focused model calls, while deterministic code handles repeatable procedures.

- **Functions:** bounded JSON capabilities callable by agents and other skills, such as parsing a particular workbook format.
- **Services:** workflows with one required schedule, such as daily briefings or weekly reports. They run only through the scheduler.
- **Applications:** persistent ASGI applications with a sandboxed interface inside Eidolon.

See the [skill lifecycle](docs/skills/lifecycle.md) for the build and validation workflow.

## System capabilities

- **Act and Observer:** additional resumable conversations for practical action and read-only investigation. Assistant shares Act's workspace, search, and browser capabilities.
- **Shared memory:** editable facts outside Atlas, daily summaries, and retained conversations in one `memory/` root, browsable through the Memory page.
- **Integrations:** Notion, Google Calendar, Google Drive, Gmail, Outlook, GitHub, and other providers. Telegram carries conversations, notifications, and approvals; WeCom provides an Observer channel.
- **WhatsApp:** an optional [linked-device bridge](docs/integrations/whatsapp.md) for cached message reads and approved text sends. Incoming messages do not activate agents; live account verification remains pending.
- **Codex tools:** an explicitly enabled [local MCP connection](docs/integrations/codex_mcp.md) exposes eligible integration operations and installed functions to host Codex sessions.
- **Activity:** inspect execution history, logs, approval requests, and automation results.

Application state, skill packages, and workspace files stay on your machine. Model calls and connected services send the data needed for their work to external providers.

## Permissions and execution

The backend enforces agent policies, integration authorization, and per-call approval for high-risk operations. Credentials stay outside model context and generated code. Skill generation, installation, and runtime approval are separate decisions; newly installed services start paused.

Generated skills run in Docker by default. An explicit local/development fallback provides less isolation, and domain-level network egress filtering is not yet implemented. See [permissions](docs/security/permissions.md) and [sandbox boundaries](docs/security/sandbox_execution.md).

## Project status

Eidolon is under active development for local, single-user use. Proactive work and scheduled services require the backend to remain running. Autonomous rewriting of application code and skill implementations is not supported.

See the [roadmap](docs/todo.md) for unfinished work and [working history](docs/working_history.md) for completed milestones.

## Local setup

Windows development requires Python 3.12+, Node.js 20+, Codex CLI, and Docker Desktop for the default skill sandbox. Optional Atlas and WhatsApp connections require Node.js 24+; the latter also needs its [bridge dependencies](docs/integrations/whatsapp.md#setup).

<details>
<summary>Development setup</summary>

From the checkout, create the Python environment and start the backend:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".\backend[dev]"
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

In a second terminal, from the checkout:

```powershell
cd frontend
npm install
npm run dev
```

Open [localhost:5174](http://localhost:5174); the API defaults to [localhost:8000](http://localhost:8000). Configure models, permissions, and connections in Settings.

No repository `.env` file is required. Configuration uses process environment variables; existing `PERSONAL_AGENT_*` names remain supported.

</details>

For technical details, start with the [documentation index](docs/README.md). Development rules and verification commands are in [AGENTS.md](AGENTS.md).
