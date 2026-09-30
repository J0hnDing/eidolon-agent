<div align="center">

# Eidolon

### A local-first personal AI agent system built around reusable skills and explicit control.

[![Status](https://img.shields.io/badge/status-active%20development-f59e0b?style=for-the-badge)](#project-status)
[![Local first](https://img.shields.io/badge/local--first-yes-14b8a6?style=for-the-badge)](#system-capabilities)
[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](backend/pyproject.toml)
[![React](https://img.shields.io/badge/React-18-61DAFB?style=for-the-badge&logo=react&logoColor=111827)](frontend/package.json)
[![License](https://img.shields.io/badge/license-Apache--2.0-6366f1?style=for-the-badge)](LICENSE)

Eidolon is a local-first control plane for personal agents, custom skills, integrations, and automations. User-defined context grounds its work; backend policy governs execution and access.

</div>

## Reusable skills

Repeated work often contains a stable procedure and a smaller part that requires judgment. Asking a language model to reconstruct the entire procedure on every run adds cost, latency, and opportunities for mistakes. Parsing data, calculating metrics, validating records, and publishing reports can instead be implemented as tested code.

**Custom skills are the core of Eidolon.** Define a recurring workflow in Project mode, review its scope, and have Eidolon build and test a reusable Python package. Approved skills take one of three forms:

- **Function:** a capability with defined inputs and outputs, callable by agents and other skills.
- **Scheduled service:** a workflow executed on a user-configured schedule.
- **Application:** a custom interface hosted inside Eidolon.

Skills use approved integrations and can delegate specific reasoning tasks to a model. Code handles deterministic operations consistently; AI handles interpretation where needed. Fully deterministic services can run without any model calls.

This creates a reusable capability library for both agent-directed work and scheduled automation.

## Use cases

The following illustrate custom skills, browser-based agent work, and scheduled automation:

- **Analysis of a specific Excel format.** A monthly operations workbook has fixed sheets, merged headers, custom status codes, and free-text exception notes. A custom skill can encode that layout, validate records, calculate agreed metrics, and flag rule violations. It then asks AI only to interpret the notes associated with those exceptions. The parsing and calculations are implemented and tested once, so subsequent workbooks follow the same procedure efficiently. The model receives a focused analysis task rather than repeatedly reconstructing the workbook and its business rules.
- **Web workflows without a dedicated integration.** Act can operate through a site's browser interface, using relevant user-defined information from Atlas and backend-managed authentication. Workflows can include submitting a prepared assignment to a course's MarkUs instance or completing an event registration. A provider-specific API integration is not necessary for every site. Execution depends on site access and available authentication; automatic credential entry currently supports configured U of T Weblogin origins, while other sites need an existing session or manual login. Secrets remain outside model context, and interactive MFA remains a user step.
- **Scheduled personal reporting.** The existing weekly summary service combines Notion todos, Atlas goal progress, and calendar commitments into a report with defined metrics and trends, without LLM calls. Research digest services can add narrow AI analysis while code handles collection, deduplication, publication, and notifications.

Custom skills are created deliberately through Project mode. Their inputs, analysis rules, integrations, and permissions are reviewed before use.

## System capabilities

The control plane provides the context and infrastructure for these workflows:

- **Persistent agents.** Act carries out work, Observer helps inspect and understand existing information, and Assistant reviews priorities and proposes useful next actions. Each has resumable conversations and a distinct permission policy.
- **User-defined personal context.** [Eidolon-Atlas](docs/integrations/atlas.md) holds explicit goals, projects, interests, experiences, relationships, and knowledge. Agents and skills can consult this editable context without having to infer a profile from conversation history.
- **Workspace and memory.** Agents share a local workspace with dedicated memory and knowledge areas. Act can keep explicit memories you request or approve; Eidolon also provides editable, deletable memory facts.
- **Integrations.** Connect supported operations from tools such as Notion, Google Calendar, Google Drive, Gmail, Outlook, GitHub, and Atlas. Telegram supports agent conversations, notifications, and approvals; WeCom provides an Observer conversation channel.
- **Schedules and automations.** Choose when services run, pause them, and inspect their results. Routine execution belongs to the backend scheduler and the installed skill's approved contract.
- **Credential management.** Configure supported connections through Eidolon. The backend manages credentials and authenticated operations, keeping secrets out of model context and generated skill code.

Local-first means the control plane, skill packages, workspace, and application state live on your machine. Model calls and connected services can still send the data needed for their work to external providers. The current agent workflows use Codex; integrations are configured as you need them.

## Backend authority and permissions

**The LLM is a tool within Eidolon. The backend controls the system.** Models can reason, propose actions, generate code, and request capabilities. Backend policy determines which requests are permitted and how they execute.

The permission system is defined in code and enforced at execution:

- Skills declare their needs. The backend reviews the actual generated package, its dependencies, and requested integration operations before runtime approval.
- Approval to build, approval to install, and permission to execute are separate decisions. A newly installed service starts paused; installation does not start an automation.
- Agent roles have distinct policies. Calls are checked against current permissions, integration authorization, resource scope, and risk; high-risk integration operations require approval for the individual call.
- Generated skills run in Docker by default. They cannot grant themselves permissions, read credentials, or gain arbitrary shell or filesystem access.
- Runs, approvals, logs, and skill versions remain inspectable. Application-managed updates are built and validated as drafts before activation.

The isolation model is still evolving: approved server-side network domains are declared and reviewed, but domain-level egress filtering is not yet implemented. An explicit local/development runtime fallback also provides less isolation than Docker. See the [permission model](docs/security/permissions.md) and [sandbox boundaries](docs/security/sandbox_execution.md) for the current guarantees and limits.

## Project status

Eidolon is under active development as a local, single-user system. The skill lifecycle, persistent agents, integrations, scheduling, explicit memory, and credential management are implemented.

**Self-assessment and improvement are planned for later.** The direction is to evaluate outcomes and propose evidence-backed changes to skills and behavior, with review before adoption. Assistant's current assessments concern your todos, goals, and opportunities; they do not mean Eidolon evaluates or improves itself. Automatic selection of stored memory facts for agent workflows is also unfinished.

See the [roadmap](docs/todo.md) for confirmed unfinished work and the [working history](docs/working_history.md) for completed milestones.

## Local setup

The current setup targets local Windows development. Requirements are Python 3.12+, Node.js 20+, Codex CLI for agent and skill-building workflows, and Docker Desktop for the default skill sandbox. Atlas is a separate optional local companion and requires Node.js 24+ when used.

<details>
<summary>Development setup</summary>

From your checkout, create the Python environment and start the backend:

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

Open [localhost:5174](http://localhost:5174). The API defaults to [localhost:8000](http://localhost:8000). Configure the connections you want in Settings.

No repository `.env` file is required for the default setup. Configuration uses process environment variables; existing `PERSONAL_AGENT_*` names remain supported.

</details>

For implementation details, start with the [documentation index](docs/README.md). For development guardrails and verification commands, read [AGENTS.md](AGENTS.md).

---

<div align="center">
  <sub>Eidolon is built around a simple rule: capability may compound; authority must remain explicit.</sub>
</div>
