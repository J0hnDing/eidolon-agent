# Scheduling

Scheduling uses APScheduler through `SchedulerService` and is exclusive to services.

Generated `service` skills are bounded JSON stdin/stdout endpoints with exactly one required `SkillSchedule`. Functions and web applications cannot declare or create schedules. A service is not a function-catalog entry, an MCP tool, or a callable target for another agent or skill.

The backend also registers the platform-owned `backend.notion.todo.cleanup_done` service every time it starts. It runs daily at 03:00 `America/Toronto`, uses a stable replacement job id, coalesces missed runs, and permits only one concurrent instance. `GET /schedules` includes a read-only platform projection with next/last state. It is not a skill, a function-catalog entry, or a mutable `SkillSchedule` row.

Startup also reconciles checked-in installed skill packages before loading scheduler rows. The installed `weekly_report_service` therefore reliably appears with its one initially paused schedule: Monday at 08:00 `America/Toronto`. It deterministically calls `github_atlas_project_scout` with `{limit: 10, period: "weekly", atlas_keywords: []}`, validates the exact repository output, formats native Notion blocks, and calls `notion.report.create`. It never calls Codex directly; any bounded analysis remains owned by the Scout function. Scout, validation, or Notion failures fail the service run rather than falling back to model-generated content.

## Schedule Types

Supported forms are:

- daily at HH:MM;
- weekly on a weekday plus HH:MM;
- interval every N minutes, hours, or days.

Each schedule also stores one JSON object input that must validate against the active service input schema.

## Creation and State

ProductManager includes required initial schedule intent only when `runtime = service`. During installation, the backend creates one paused schedule from the installed manifest. Installation never activates it automatically.

The schedule row becomes backend-owned runtime state after installation. Users edit timing, timezone, and input on the shared Schedules page. Version updates do not replace those edits; activation fails if the existing input is incompatible with the candidate service schema.

Canonical statuses are `active` and `paused`. There is no separate enabled state or schedule-approval lifecycle for services. Runtime permission and integration approvals remain independent and are checked before a paused schedule can be resumed.

## Execution

Automatic execution occurs only while the schedule is active. Run Now is allowed for either active or paused generated-service schedules and does not resume a paused schedule.

Every run rechecks that:

- the skill is installed and still uses `runtime = service`;
- the schedule input matches the active manifest schema;
- runtime permissions and declared integration authorizations are current;
- the active service version is available to the bounded runner;
- no overlapping operation is active for that skill.

The runner creates an attributed `skill_runs` row with `invocation_source = schedule` and `source_schedule_id`. Its ephemeral runtime capability may call only functions, integrations, and Codex declared by the active manifest. The service itself remains unavailable as a callable endpoint outside the scheduler.

Top-level `status: "partial"` or `status: "failed"` output remains a matching backend run result instead of being treated as success merely because the process exited with valid JSON.
