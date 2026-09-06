# Scheduling

Scheduling uses APScheduler through `SchedulerService` and is exclusive to services.

Generated `service` skills are bounded JSON stdin/stdout endpoints with exactly one required `SkillSchedule`. Functions and web applications cannot declare or create schedules. A service is not a function-catalog entry, an MCP tool, or a callable target for another agent or skill.

The backend registers a platform-service schedule registry every time it starts. It contains `backend.notion.todo.cleanup_done` daily at 03:00 and `backend.quercus.knowledge.sync` daily at 10:00, both in `America/Toronto`. Registry values are defaults; each service's enabled state, display name, and edited recurrence are durable runtime state. Each uses a stable replacement job id, coalesces missed runs, and permits only one concurrent instance. `GET /schedules` includes platform projections with durable next/last state and an `is_running` projection derived from active occurrences. Generated schedule rows derive the same field from active attributed runs or occurrences. Platform services are not skills, function-catalog entries, MCP tools, or mutable `SkillSchedule` rows.

The Assistant assessment is a separate backend-owned interval service. It is disabled by default and, when enabled, establishes a persistent anchor whose first automatic run is 72 hours later. Disabling clears the pending automatic time; re-enabling establishes a new anchor instead of replaying the disabled period. Run Now creates a fresh Assistant session without changing the automatic anchor. Automatic and startup catch-up runs use the durable occurrence ledger, claim only the latest missed 72-hour occurrence, and never retry a claimed failure or capacity block. A successful dispatch records `queued` because the assessment continues as an ordinary Assistant turn. If the five-session Assistant retention limit cannot make room because the oldest session is busy, the occurrence records `blocked` and the next 72-hour occurrence remains eligible normally.

Each assessment asks Assistant to proactively identify concrete, worthwhile ways Act can help based on current and recent todos, goals, commitments, deadlines, and the user's broader situation. It may gather useful context from Eidolon functions, read-only workspace files (notably `knowledge/assistant`), and internet search; it should infer intent from evidence, state material assumptions, ask only critical clarifications, and prefer high-value, timely, specific interventions. It reads prior proposal history before proposing, uses a private approval request only for a concrete plan with the exact Act instruction, avoids materially similar proposals unless circumstances materially changed, and remains quiet when it finds no sufficiently useful opportunity. Approval and any subsequent Act execution are independent of the scheduler occurrence.

The assessment appears in Schedules even while disabled. Its Enable/Disable and Run Now buttons call the same assessment endpoints used by Agents → Assistant, and Edit opens that Agent surface. After the initial assessment turn finishes, the notification bot sends only `Assessment Success` or `Assessment Fail` and `You have X proposals.` The connected Assistant bot delivers the full response and selects that assessment thread so replies continue it. Follow-up turns do not repeat the assessment notification. If the Assistant bot is disconnected, the conversation remains available in the Agents UI. Notification failure is logged without changing the assessment result.

Startup also reconciles checked-in installed skill packages before loading scheduler rows. The installed `weekly_report_service` therefore reliably appears with its one initially disabled schedule: Monday at 08:00 `America/Toronto`. It deterministically calls `github_atlas_project_scout` with `{limit: 10, period: "weekly", atlas_keywords: []}`, validates the exact repository output, formats native Notion blocks, and calls `notion.report.create`. Version v2 adds `telegram.notification.send`: after all reports are created, it sends `Your weekly reports are ready` with a list of the created reports. A failed run attempts a Telegram alert titled `Weekly report failed` whose description contains the normalized error code and message; an alert-delivery failure never replaces the original run failure. Because Telegram is a new integration requirement, v2 remains inactive until its runtime approval is granted and the version is activated. The service never calls Codex directly; any bounded analysis remains owned by the Scout function. Scout, validation, Notion, or completion-notification failures fail the service run rather than falling back to model-generated content.

## Schedule Types

Supported forms are:

- daily at HH:MM;
- weekly on a weekday plus HH:MM;
- interval every N minutes, hours, or days.

Each schedule also stores one JSON object input that must validate against the active service input schema.

## Creation and State

ProductManager includes required initial schedule intent only when `runtime = service`. During installation, the backend creates one disabled service with one paused compatibility schedule row from the installed manifest. Installation never enables it automatically.

The schedule row becomes backend-owned runtime state after installation. Users edit timing, timezone, and input on the shared Schedules page. Version updates do not replace those edits; activation fails if the existing input is incompatible with the candidate service schema.

Backend-owned platform services expose the same Enable/Disable, Run Now, and Edit actions on Schedules. Their edited names and recurrence definitions survive restart, disabling removes the scheduler job without deleting the service, and re-enabling establishes a fresh activation boundary. New registry entries receive these actions automatically.

`Skill.enabled` is the canonical service-availability control. Enabling a service activates and registers its required schedule; disabling it pauses and unregisters that schedule. Existing `active`/`paused` schedule rows are reconciled into enabled state at startup for compatibility. Runtime permission and integration approvals remain independent and are checked before a service can be enabled.

## Execution

Automatic execution and Run Now are allowed only while the service is enabled and its required schedule is active. Run Now does not change availability.

Every run rechecks that:

- the skill is installed and still uses `runtime = service`;
- the schedule input matches the active manifest schema;
- runtime permissions and declared integration authorizations are current;
- the active service version is available to the bounded runner;
- no overlapping operation is active for that skill.

The runner creates an attributed `skill_runs` row with `invocation_source = schedule` and `source_schedule_id`. Its ephemeral runtime capability may call only functions, integrations, and Codex declared by the active manifest. The service itself remains unavailable as a callable endpoint outside the scheduler.

Top-level `status: "partial"` or `status: "failed"` output remains a matching backend run result instead of being treated as success merely because the process exited with valid JSON.

## Latest Missed Occurrence

Eidolon gives every intended scheduled time a deterministic idempotency key derived from the stable schedule key, the current schedule-definition fingerprint, and the intended UTC fire time. A durable `schedule_occurrences` claim is committed before the service is invoked. That claim is the at-most-once boundary: an occurrence is never invoked again after it has been claimed, whether its result is succeeded, partial, failed, blocked, or interrupted.

On startup, Eidolon computes the latest intended occurrence at or before one shared startup timestamp for every active generated-service and platform schedule. If that exact occurrence has no durable claim, Eidolon queues one immediate `startup_catch_up` execution. Older missed occurrences are coalesced and never replayed. Failed or interrupted occurrences are not retried. A later intended occurrence still receives its own key and may run normally.

Activation boundaries prevent unwanted backfill:

- creating a disabled service does not create occurrences;
- enabling establishes a new active boundary, so times during the disabled period are not run;
- editing timing, timezone, or input establishes a new definition fingerprint and active boundary;
- interval schedules persist their first-fire anchor so their intended times do not drift across restarts.

Normal APScheduler callbacks and startup catch-ups use the same claim path, so a startup race cannot invoke the same intended time twice. Explicit Run Now actions remain user-triggered and do not consume or reuse scheduled occurrence keys.

Scheduled `skill_runs` expose `schedule_occurrence_key`, `scheduled_for_at`, and `schedule_trigger`. The same non-secret key is provided to local and Docker service entrypoints as `PERSONAL_AGENT_SCHEDULE_IDEMPOTENCY_KEY` so service code can pass it to an external API that supports idempotency. Because the claim is committed before invocation and failures are never retried, the contract is at most once: a crash can leave an occurrence uncompleted, but cannot cause Eidolon to execute it again.
