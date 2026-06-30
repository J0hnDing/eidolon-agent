# AGENTS.md

## Project Name

Local-First Self-Extending Personal AI Assistant

## Project Goal

Build a local-first AI assistant that can understand a user's interests, goals, preferences, and repeated needs, then create reusable "skills" to help the user. A skill is a reusable capability package that can be installed, inspected, enabled, disabled, edited, or deleted by the user.

A skill can be:

1. `instruction` - reusable instructions only, with no executable automation code.
2. `automation` - executable automation code.
3. `hybrid` - both reusable instructions and executable automation code.

The assistant should eventually be able to:

1. Chat with the user through a UI.
2. Store editable user memory, such as interests, goals, preferred sources, and communication preferences.
3. Decide whether a request should be answered directly, handled by an existing skill, or turned into a new reusable skill.
4. Generate new skill code using Codex.
5. Validate, test, and sandbox generated skills before installation.
6. Ask for user approval when a skill requests risky permissions.
7. Show all installed/proposed/disabled skills in a UI.
8. Let the user inspect skill code, permissions, logs, and run history.
9. Let the user delete, disable, or edit skills.
10. Run approved skills manually or on a schedule.

This is not just a chatbot. The core product is a controlled platform where AI converts repeated user needs into safe, reusable capability packages.

---

## Current MVP Decisions

These decisions supersede older milestone wording when there is a conflict:

1. Chat has explicit modes:
   - `chat` mode is normal conversation and must not create skills.
   - `project` mode is the only mode that may propose or generate application skills.
2. Do not use keyword heuristics to decide whether normal chat should become a skill. The user chooses project mode explicitly.
3. In project mode, the backend must evaluate whether the requested project is plausible before creating a skill generation plan. If it is not plausible, explain why and suggest safer or better-scoped project ideas.
4. Skill generation requires build-time permission approval before Codex writes files.
5. Installing or running a generated skill requires runtime permission review based on the actual generated `manifest.json`, not only the initial plan.
6. Approval to generate does not approve installation. Approval to install does not approve automatic execution. Skills never run automatically in the MVP.
7. Runtime network domains may be approved as declared design intent, but the current local runner cannot enforce domain-level network sandboxing. Networked skills must remain blocked from execution until sandboxing exists.
8. Skill deletion is a hard delete in the local MVP: remove the controlled skill folder and remove the skill database record. Do not leave deleted skills visible in the normal Skills list.
9. Installed skill folders found on disk under `skills/installed/<skill_name>/` may be registered into the local database if their manifest is valid. This keeps filesystem state and the UI list from drifting apart.
10. A user-facing tool is not a new `skill_type`. It is an installed runnable automation or hybrid skill with `interface_type = "tool"`. Codex may design a tool UI by writing declarative `tool_ui_schema` JSON in `manifest.json`; generated skills must not inject React, HTML, JavaScript, or app frontend code.
11. Skill status `building` means an agent workflow is creating or repairing the skill. Status `proposed` means generation is complete and the skill is waiting for user review, runtime permission approval, installation, or rejection.
12. Build-time and runtime approval prompts should appear inline in the Chat transcript as assistant messages with approve/decline actions. They should not be route-local modal state that disappears when the user switches pages.
13. Approving build-time permissions from either Chat or the Approval Requests page must mark the linked generation request approved so the agent workflow can resume safely.

---

## Non-Negotiable Design Principles

### 1. Local-first

The first version should run locally on the user's machine. Do not build a multi-user SaaS architecture in the MVP.

### 2. User owns memory

User memory must be explicit, editable, and deletable. Do not silently collect everything.

Memory should store actionable facts only, such as:

- Interests
- Goals
- Preferences
- Trusted sources
- Blocked sources
- Writing style preferences
- Risk tolerance
- Repeated routines

Do not store sensitive personal data unless the user explicitly asks.

### 3. AI may generate skills, but the platform controls execution

Codex may generate skill code, tests, manifests, and documentation, but the application must control:

- Where generated files are written
- What permissions a skill requests
- Whether tests pass
- Whether the skill can access network/files/secrets
- Whether the user has approved risky actions
- Whether the skill is enabled, disabled, updated, or deleted

### 4. Generated code must be constrained

Generated skills must live only inside controlled folders.

Allowed generated-skill locations:

```text
skills/proposed/<skill_name>/
skills/installed/<skill_name>/
```

Generated skills must not write outside their own runtime/cache directories unless explicitly approved.

### 5. No arbitrary dangerous execution

Do not implement general-purpose arbitrary shell execution in the MVP.

Block or require explicit approval for:

- Shell commands
- Reading arbitrary user files
- Deleting files
- Sending emails
- Posting online
- Making purchases
- Trading financial assets
- Accessing browser cookies
- Reading SSH keys
- Uploading private data
- Installing packages silently
- Accessing credentials or secrets

### 6. Test before install

Every generated skill must include tests. A proposed skill cannot be installed unless:

1. Its manifest is valid.
2. Its requested permissions are understood.
3. Its tests pass.
4. Its risk level is classified.
5. The user approves installation when approval is required.

### 7. Permissions are explicit

Every skill must declare permissions in `manifest.json`.

Examples:

```json
{
  "permissions": {
    "network": ["example.com"],
    "filesystem_read": [],
    "filesystem_write": ["./cache"],
    "secrets": [],
    "shell": false
  }
}
```

A skill must never receive full filesystem or full network access by default.

### 8. UI must expose control

The UI must show:

- Installed skills
- Proposed skills
- Disabled skills
- Failed skills
- Skill permissions
- Skill code
- Test results
- Run history
- Logs
- Delete / disable / run buttons
- Approval requests

---

## Recommended Tech Stack

Use this stack unless the user explicitly changes it:

```text
Frontend: React, optionally Tauri later
Backend: Python FastAPI
Database: SQLite
Skill language: Python
Testing: pytest
Skill execution: local subprocess first, Docker sandbox later
Scheduler: APScheduler
Codex integration: Codex CLI / codex exec
Version control: Git
```

The first milestone should not require Docker, Tauri, or complex browser automation. Start simple and make the core model correct.

---

## Core Concepts

### Assistant Brain

The assistant brain interprets user requests and routes them into one of these categories:

```text
DIRECT_ANSWER
USE_EXISTING_SKILL
CREATE_NEW_SKILL
MODIFY_EXISTING_SKILL
REQUEST_APPROVAL
UNSAFE_OR_UNSUPPORTED
```

In the current MVP, the user explicitly chooses `chat` or `project` mode in the UI. Only `project` mode can create a skill proposal. Project mode must run a plausibility review before creating a generation plan. Later, the router can become more capable, but it must not silently turn ordinary chat into skill generation.

### Agent Workflow Roles

Milestone 10 uses a bounded agent workflow. Agents are not free-running autonomous actors. They are role-specific workflow steps controlled by the backend.

The MVP agent roles are:

```text
product_manager
builder
tester
security_reviewer
```

There are no separate `planner`, `reviewer`, `permission_analyst`, or `repairer` agents in the MVP.

- `ProductManagerAgent` replaces planner/reviewer behavior.
- `SecurityReviewerAgent` replaces permission-analyst behavior.
- `BuilderAgent` handles build, update, and repair modes.
- `TesterAgent` owns test creation, test maintenance, validation, and test execution.

Agents communicate only through structured workflow artifacts stored by the platform:

```text
agent_runs
agent_run_steps
blueprint_json
milestone_json
decision_json
test_result_json
failure_log
security_review_json
user_summary
```

Agents must not freely chat with each other, spawn other agents, run indefinitely, install skills, run skills, approve permissions, or bypass backend safety checks.

#### ProductManagerAgent

ProductManagerAgent owns workflow direction and user-facing project judgment.

It runs at these decision points:

1. At the start of a build, update, or repair workflow.
2. After a milestone's tests pass.
3. After all planned milestones are complete.
4. After repeated failures on one milestone.
5. When the workflow is blocked or needs user input.

Responsibilities:

```text
- understand the user's request
- decide whether the project is plausible and safe enough to attempt
- write the project blueprint
- break the work into explicit milestones
- define acceptance criteria for each milestone
- decide the next workflow action
- summarize progress for the user at approval and completion checkpoints
- decide whether the project is complete after milestone tests pass
- request SecurityReviewerAgent review before build-time or runtime approval prompts
- stop the workflow when the request is unsafe, unsupported, unclear, or repeatedly failing
```

Allowed ProductManager decisions:

```text
request_permission
build_next_milestone
run_tests
repair_current_milestone
ask_user_for_input
finish_ready_for_review
stop_failed
stop_unsupported
```

ProductManagerAgent must not:

```text
write implementation code
edit generated skill files directly
approve permissions
install skills
run skills
bypass failed tests
bypass SecurityReviewerAgent
```

#### BuilderAgent

BuilderAgent writes and modifies skill files inside controlled skill folders.

Builder modes:

```text
build
repair
update
```

Responsibilities:

```text
- implement the current ProductManager milestone only
- create or edit generated skill files under skills/proposed/<skill_name>/
- create or update manifest.json, README.md, SKILL.md, skill.py, and support files as assigned
- If runtime errors occurs, log the error and attempt to fix. After three unsucessful fixes, pause, notify user. 
- fix implementation bugs reported by TesterAgent
- keep changes consistent with AGENTS.md, the manifest schema, and milestone acceptance criteria
- identify when a bug or blocker requires user intervention
```

If BuilderAgent detects that user intervention is required, it must stop and produce a user-action-required report.

Examples requiring user intervention:

```text
missing API key or secret
external login required
unsupported runtime permission
package installation needed but not approved/supported
unclear product requirement
conflict between user request and AGENTS.md safety rules
network/domain access required but current runtime sandbox cannot support it
file access requiring a user-selected path
```

The user-action-required report must include:

```text
exact blocker
why BuilderAgent cannot safely continue
specific user step needed
whether the workflow can resume after user action
files, dependencies, or permissions involved
```

BuilderAgent must not:

```text
install dependencies automatically
approve permissions
install skills
run skills
edit backend/frontend app source code while building an application skill
grant itself permissions through manifest changes without SecurityReviewerAgent review
```

#### TesterAgent

TesterAgent validates whether the generated skill satisfies the blueprint and milestone acceptance criteria.
TesterAgent is Codex-backed for test authoring: it should inspect the blueprint and Builder-created files, then write rich but not overly complicated pytest tests. The backend still owns deterministic validation and test execution.

Responsibilities:

```text
- read the ProductManager blueprint and milestone acceptance criteria
- inspect Builder-created skill files before writing tests
- create or update pytest tests for automation/hybrid skills when needed
- validate manifest schema
- run tests through the existing safe validation/test path
- check the JSON stdin/stdout contract for executable skills
- report failures clearly
- record failure logs in agent_run_steps and, where practical, a readable failure log artifact
```

TesterAgent may write or update test files. TesterAgent must not edit implementation code.

TesterAgent must not:

```text
patch skill.py or implementation files
approve permissions
install dependencies
install skills
run skills outside the approved validation/test path
ignore failing tests
```

#### SecurityReviewerAgent

SecurityReviewerAgent handles permissions, risk, and runtime support review.

Responsibilities:

```text
- analyze build-time permissions from the ProductManager blueprint
- analyze runtime permissions from the generated manifest.json
- detect permission expansion between plan and actual manifest
- classify risk level
- identify blocked or unsupported permissions
- create or update approval requests
- explain what each requested permission is for
- explain what approval allows and does not allow
- decide whether permissions are supported by the current runner
```

SecurityReviewerAgent must produce a user-facing security summary for build-time and runtime approval checkpoints.

SecurityReviewerAgent must not:

```text
approve permissions
weaken runner restrictions
install skills
run skills
ignore permission expansion
allow blocked permissions in the MVP
```

Blocked or unsupported in the MVP:

```text
shell access
secrets
broad filesystem access
unrestricted network access
browser automation
email/calendar/finance actions
public posting
purchases
trading
file deletion
```

Runtime network domains may be approved as declared design intent, but execution must remain blocked until sandboxing can enforce network access.

#### Combined Approval Summary

At approval checkpoints, the UI should show a combined two-part summary.

Part A: ProductManager summary

```text
what will be built
why it is useful
milestone plan
expected files
expected behavior
what approval allows
what approval does not allow
```

Part B: SecurityReviewer summary

```text
requested permissions
requested dependencies
requested network domains
filesystem access
blocked or unsupported items
risk level
why each permission is needed
```

The same two-part pattern applies to runtime permission review after generation:

```text
ProductManager: whether the generated package appears complete and what it does
SecurityReviewer: actual manifest permissions, risk, blocked items, and runtime support
```

In the current MVP, these summaries should be rendered inline in the Chat page as assistant messages:

```text
user project request
-> assistant message with ProductManager blueprint summary and SecurityReviewer build-time approval summary
-> inline Approve Generation / Decline buttons
-> assistant generation progress and completion message
-> assistant message with ProductManager completion summary and SecurityReviewer runtime permission summary
-> inline Approve Runtime Permissions / Deny buttons
```

The inline approval messages must remain in the local chat history when the user navigates away and returns. While a project build is running in that chat window, additional user input in the same window may be ignored or disabled until the build and summaries complete.

Permission expansion should only be shown when the actual generated manifest requests permissions that are meaningfully greater than the approved build-time plan. An empty expansion object must not be presented as a warning.

### Memory

Memory is a set of explicit records.

Suggested table fields:

```text
id
key
value
category
source_message_id
sensitivity
created_at
updated_at
expires_at
user_editable
```

Suggested categories:

```text
interests
goals
preferences
routines
trusted_sources
blocked_sources
writing_style
risk_tolerance
```

### Skill

A skill is a reusable capability package.

Skill types:

```text
instruction - reusable instructions only, no executable code
automation - executable automation code
hybrid - instructions plus executable automation code
```

A skill folder should look like:

```text
skills/
  proposed/
    ai_news_digest/
      manifest.json
      skill.py
      README.md
      tests/
        test_skill.py
      cache/
  installed/
    ai_news_digest/
      manifest.json
      skill.py
      README.md
      tests/
        test_skill.py
      cache/
```

Every skill must have:

```text
manifest.json
README.md
```

Automation and hybrid skills must also have:

```text
skill.py
tests/
```

Instruction and hybrid skills must declare an `instructions_path` in `manifest.json`.

### Skill Manifest

The manifest is the source of truth for the skill.

Example:

```json
{
  "name": "ai_news_digest",
  "description": "Summarizes AI infrastructure news relevant to the user.",
  "skill_type": "automation",
  "interface_type": "chat",
  "entrypoint": "skill.py",
  "instructions_path": null,
  "input_schema": null,
  "output_schema": null,
  "tool_ui_schema": null,
  "risk_level": "low",
  "permissions": {
    "network": ["reuters.com", "apnews.com", "nvidia.com", "amd.com"],
    "filesystem_read": [],
    "filesystem_write": ["./cache"],
    "secrets": [],
    "shell": false
  },
  "schedule": null,
  "created_by": "codex",
  "enabled": false
}
```

### Skill Input and Output

Skills should accept JSON input and return JSON output.

Example input:

```json
{
  "topics": ["AI infrastructure", "Nvidia", "AMD", "Broadcom", "optical networking"],
  "max_items": 10,
  "trusted_sources": ["Reuters", "AP", "company press releases"]
}
```

Example output:

```json
{
  "title": "AI Infrastructure News Digest",
  "items": [
    {
      "headline": "Example headline",
      "source": "Example source",
      "url": "https://example.com/article",
      "summary": "Short summary.",
      "why_it_matters": "Short explanation."
    }
  ],
  "warnings": []
}
```

---

## Suggested Database Tables

Use SQLAlchemy or SQLModel.

Minimum tables:

```text
messages
memory_facts
skills
skill_versions
skill_runs
approval_requests
```

### messages

```text
id
role
content
created_at
conversation_id
```

### memory_facts

```text
id
key
value
category
source_message_id
sensitivity
created_at
updated_at
expires_at
user_editable
```

### skills

```text
id
name
description
skill_type
status
risk_level
manifest_path
instructions_path
installed_path
created_at
updated_at
enabled
```

Status values:

```text
building
proposed
installed
disabled
failed
deleted
```

### skill_versions

```text
id
skill_id
version
manifest_json
code_snapshot_path
created_at
change_summary
```

### skill_runs

```text
id
skill_id
status
input_json
output_json
stdout
stderr
exit_code
started_at
ended_at
error_message
```

Status values:

```text
pending
running
succeeded
failed
blocked
```

### approval_requests

```text
id
skill_id nullable
generation_request_id nullable
request_scope
request_type
risk_level
requested_permissions_json
requested_dependencies_json
requested_network_domains_json
requested_filesystem_json
reason_json
reason
user_explanation
status
created_at
resolved_at
resolved_by
decision_notes
```

Status values:

```text
pending
approved
denied
expired
superseded
```

Request scopes:

```text
build_time
runtime
```

Risk levels may also include:

```text
blocked
```

---

## Permission Risk Levels

### Low Risk

Examples:

```text
Read approved RSS feeds
Fetch public web pages from allowlisted domains
Write to the skill's own cache folder
Summarize public text
```

Low-risk skills may require one-time approval.

### Medium Risk

Examples:

```text
Scrape websites
Install packages
Read user-selected folders
Run scheduled jobs
Use browser automation
```

Medium-risk skills require explicit approval before install.

### High Risk

Examples:

```text
Send emails
Modify calendar
Delete files
Make purchases
Trade stocks
Post publicly
Use credentials
Access private files
Run shell commands
```

High-risk actions require per-action approval.

### Prohibited in MVP

Do not implement these in the MVP:

```text
Financial trading execution
Autonomous purchases
Autonomous public posting
Reading browser cookies
Reading SSH keys
Deleting broad directories
Full filesystem access
Unrestricted shell access
Unrestricted internet access
```

---

## Milestone Plan

### Milestone 1: Local Backend Foundation

Build:

1. FastAPI backend.
2. SQLite database.
3. Models for messages, memory facts, skills, skill versions, skill runs, and approval requests.
4. Basic CRUD endpoints.
5. Manifest schema validator.
6. Unit tests for manifest validation.

Do not implement Codex integration yet.
Do not implement arbitrary code execution yet.
Do not implement Docker yet.

### Milestone 2: Basic Frontend

Build:

1. React frontend.
2. Chat page.
3. Memory page.
4. Skills page.
5. Skill detail page.
6. Approval request page or modal.
7. Run history view.

The UI may use mock data at first, then connect to backend endpoints.

### Milestone 3: Manual Skill Runner

Build a local runner for demo skills.

The runner should:

1. Read `manifest.json`.
2. Validate the manifest.
3. Run pytest inside the skill folder.
4. Run the skill entrypoint with JSON input.
5. Capture stdout, stderr, exit code, start time, and end time.
6. Store the run result in `skill_runs`.
7. Refuse to run skills whose manifest requests unsupported permissions.

At this stage, only run trusted demo skills.

### Milestone 4: First Demo Skill

Create a manual sample skill:

```text
personal_news_digest
```

It should:

1. Accept topics and sources as JSON input.
2. Use local/offline sample article data in the current MVP.
3. Deduplicate items.
4. Return JSON summaries.
5. Include tests.
6. Use only low-risk permissions.

Do not use live RSS fetching, arbitrary web scraping, or browser automation yet. Networked news fetching comes after sandbox/network enforcement.

### Milestone 5: Proposed Skill Workflow

Implement:

1. `skills/proposed/<skill_name>/` workspace.
2. Skill proposal records in the database.
3. Manifest validation for proposed skills.
4. Test execution for proposed skills.
5. UI display of proposed skill code, manifest, permissions, and test results.
6. Install button that moves a valid proposed skill to `skills/installed/<skill_name>/`.

### Milestone 6: Codex Skill Generation

Add Codex integration using Codex CLI / `codex exec`.

Flow:

```text
User asks for reusable automation
→ backend creates skill generation request
→ backend creates proposed skill directory
→ backend writes prompt to a file
→ backend calls codex exec in a restricted workspace
→ Codex generates manifest.json, skill.py, README.md, and tests
→ backend validates manifest
→ backend runs tests
→ UI shows proposed skill
→ user approves or rejects
```

Current implementation update:

```text
User switches to project mode
-> user asks for a reusable capability package
-> backend evaluates project plausibility
-> backend creates a generation plan
-> backend creates a build-time permission request
-> user approves or declines generation
-> backend creates proposed skill directory
-> backend writes prompt to a file
-> backend calls codex exec in a restricted workspace, or a fake/dev adapter
-> Codex generates manifest.json, README.md, and required skill files/tests
-> backend validates manifest
-> backend runs tests for automation/hybrid skills
-> backend analyzes runtime permissions from the generated manifest
-> UI shows proposed skill
-> user inspects, validates, installs, or rejects
```

Rules:

1. Codex must only write inside the proposed skill directory.
2. Codex must generate tests.
3. Codex must generate a manifest.
4. Codex must not create high-risk permissions silently.
5. Codex must not access application secrets.
6. Codex generation must not install or run the generated skill.
7. Build-time approval allows generation only; runtime permissions are reviewed separately.

### Milestone 7: Approval System

Build full approval workflow.

Approval should be required when:

1. A skill requests medium/high-risk permissions.
2. A skill wants to expand permissions.
3. A skill wants to use secrets.
4. A skill wants to run on a schedule.
5. A skill wants to execute shell commands.
6. A skill wants to read or write user-selected files.
7. A skill wants to send data outside the local machine.

Approval UI must show:

```text
Skill name
Purpose
Requested permissions
Risk level
Reason
Approve / Deny / Edit
```

The implemented approval model distinguishes:

```text
build_time - approval for Codex to generate proposed files
runtime - approval for permissions declared in the generated manifest
```

Runtime approval must be based on the generated manifest. Permission expansion from the build-time plan must be shown clearly. Blocked permissions, including shell access, secrets, broad filesystem access, and unrestricted network access, must not be approved in the MVP.

Runtime network domains can be approved as an intent recorded in the manifest, but execution must remain blocked until sandboxing can enforce network access.

### Milestone 8: Sandbox Execution

Replace or extend subprocess execution with Docker sandboxing.

Sandbox goals:

1. Limit filesystem access.
2. Limit network access.
3. Limit CPU and memory.
4. Enforce timeouts.
5. Avoid exposing secrets.
6. Capture logs.
7. Mount only approved directories.

If Docker is not available, keep a reduced local runner and label it as unsafe/dev-only.

### Milestone 9: Scheduling

Add scheduling with APScheduler.

Support:

```text
manual run
daily run
weekly run
simple recurring interval
```

Rules:

1. Scheduled runs require approval.
2. Scheduled skills must be visible in the UI.
3. Scheduled runs must store logs.
4. User can pause or delete scheduled skills.

### Milestone 10: Agentic Skill Build and Repair Workflow

Build a bounded agent workflow system for skill generation and repair.

Agent runs are tracked workflows with visible role-based steps:

```text
product_manager
security_reviewer
builder
tester
```

Rules:

1. ProductManager creates the concise blueprint, milestone plan, acceptance criteria, and user-facing summaries.
2. SecurityReviewer creates build-time and runtime approval requests and summaries, but never approves permissions.
3. Builder writes or repairs generated skill files only inside controlled skill folders.
4. Tester validates manifests, tests, and JSON stdin/stdout contracts through the existing safe validation path.
5. A build workflow pauses for one build-time approval before Builder writes files.
6. The workflow should proceed smoothly between ProductManager, SecurityReviewer, Builder, and Tester after that approval. Do not require approval merely because the workflow moves between roles.
7. If tests fail, Builder should read Tester failure output, attempt repair, and Tester should rerun validation.
8. If one milestone fails more than 3 times, ProductManager stops the workflow and writes a user-facing stuck summary.
9. Runtime permission review is based on the actual generated `manifest.json`, not just the original plan.
10. Permission expansion during build or repair must be shown clearly and require review before install/run.
11. Empty permission expansion must not be shown as a warning.
12. Agent workflows must not install skills automatically.
13. Agent workflows must not run skills automatically.
14. Repair workflows for installed skills should write to a proposed repair copy, not directly mutate the installed skill.
15. Skills under active agent construction should use status `building`. A skill should become `proposed` only after the generated package is ready for user review.
16. If ProductManager selects `interface_type = "tool"`, the blueprint and acceptance criteria must require a declarative `tool_ui_schema` so the Tools page can render a user-facing form.
17. Do not build a general-purpose agent playground in the MVP.

---

## First Real Demo Scenario

The first complete demo should support this user request:

```text
I want to follow AI infrastructure news: Nvidia, AMD, Broadcom, optical networking, and data centers. Make this a reusable daily skill.
```

Expected behavior:

1. Assistant stores relevant interests as editable memory.
2. User switches to project mode.
3. Assistant evaluates whether the request is a plausible reusable skill.
4. Assistant proposes creating a news digest skill.
5. UI shows build-time permissions and generation limits.
6. User approves generation.
7. Codex generates the skill in `skills/proposed/ai_infra_news_digest/`.
8. Skill includes manifest, code, README, and tests.
9. Backend validates the manifest.
10. Backend runs tests.
11. UI shows runtime permissions from the generated manifest.
12. User approves runtime permissions and installation if supported.
13. Skill runs manually only if runtime permissions are approved and supported.
14. UI shows digest output, logs, and sources.
15. User can disable or hard-delete the skill.

---

## Implementation Rules for Codex

When working in this repository:

1. Prefer small, testable commits.
2. Do not implement everything at once.
3. Do not introduce high-risk features unless they go through the approval system and remain blocked when unsupported.
4. Do not add browser automation or live network fetching before sandbox/network enforcement exists.
5. Do not add email/calendar/finance actions in the MVP.
6. Keep generated-skill code isolated from application code.
7. Use clear interfaces between backend, runner, and UI.
8. Write tests for validators, runners, and permission logic.
9. Keep memory user-editable.
10. Make every irreversible operation require explicit user action.
11. When uncertain, choose the safer architecture.

---

## Suggested Repository Structure

```text
personal-agent/
  AGENTS.md
  README.md
  backend/
    app/
      main.py
      db.py
      models/
      schemas/
      routers/
      services/
        manifest_validator.py
        skill_runner.py
        approval_service.py
        memory_service.py
        codex_service.py
    tests/
    pyproject.toml
  frontend/
    package.json
    src/
      pages/
      components/
      api/
  skills/
    proposed/
    installed/
  runtime/
    skill_runs/
    skill_cache/
  docs/
    skill_spec.md
    security_model.md
```

---

## Initial Build Priority

Start with Milestone 1 only.

Do not implement Codex integration in the first commit.
Do not implement arbitrary code execution in the first commit.
Do not implement Docker in the first commit.
Do not implement scheduling in the first commit.

First target:

```text
A working FastAPI backend with SQLite models, manifest schema validation, and tests.
```

After that, add the frontend and manual skill runner.

---

## Definition of Done for MVP

The MVP is done when:

1. User can chat in the UI.
2. User can add/edit/delete memory facts.
3. User can view skills.
4. User can create or view a proposed skill.
5. Proposed skills have manifest, code, README, and tests.
6. The system validates skill manifests.
7. The system runs skill tests before installation.
8. The user can approve installation.
9. The user can run an installed skill.
10. The user can inspect run logs.
11. The user can disable/delete a skill.
12. A working news digest skill exists.
13. Codex can generate a new proposed low-risk skill.
14. Risky permissions are shown clearly before approval.

---

## Important Clarification

There are two types of skills:

### Codex skills

These help Codex work better inside this repository.

### Application skills

These are user-facing reusable capability packages created, installed, and managed by the personal assistant.

Do not confuse them.

This repository is building the application skill system.
