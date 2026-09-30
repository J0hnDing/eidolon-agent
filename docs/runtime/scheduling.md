# Scheduling

Scheduling uses APScheduler through `SchedulerService` and is exclusive to services.

Generated `service` skills are bounded JSON stdin/stdout endpoints with exactly one required `SkillSchedule`. Functions and web applications cannot declare or create schedules. A service is not a function-catalog entry, an MCP tool, or a callable target for another agent or skill.

The backend registers a platform-service schedule registry every time it starts. It contains `backend.notion.todo.cleanup_done` daily at 03:00 and `backend.quercus.knowledge.sync` daily at 10:00, both in `America/Toronto`. Registry values are defaults; each service's enabled state, display name, and edited recurrence are durable runtime state. Each uses a stable replacement job id, coalesces missed runs, and permits only one concurrent instance. `GET /schedules` includes platform projections with durable next/last state and an `is_running` projection derived from active occurrences. Generated schedule rows derive the same field from active attributed runs or occurrences. Platform services are not skills, function-catalog entries, MCP tools, or mutable `SkillSchedule` rows.

The Assistant assessment is a separate backend-owned interval service with a default recurrence of every 3 days. It is disabled by default and, when enabled, establishes a persistent anchor whose first automatic run is one configured interval later. Its interval can be edited from the shared Schedules modal; editing re-anchors the next automatic run using the new interval. Disabling clears the pending automatic time; re-enabling establishes a new anchor instead of replaying the disabled period. Run Now creates a fresh Assistant session without changing the automatic anchor. Automatic and startup catch-up runs use the durable occurrence ledger, claim only the latest missed configured interval, and never retry a claimed failure or capacity block. A successful dispatch records `queued` because the assessment continues as an ordinary Assistant turn. If the five-session Assistant retention limit cannot make room because the oldest session is busy or has an unsent follow-up, the occurrence records `blocked` and the next configured interval remains eligible normally.

Each assessment asks Assistant to review todos, active goals and subgoals, and current opportunities in that order. It may gather useful context from read-only Eidolon functions, managed files, proposal history, and internet search. The first turn returns one structured result; the backend creates proposals and Goal questions. Approved function actions run directly through the invocation kernel, while approved Act actions use a linked Act session. Approval and execution are independent of the scheduler occurrence.

Every valid assessment writes one Notion Opportunity Scout report from the ordered opportunity titles and descriptions, including an empty list when none qualify. Reporting needs no approval. If Notion is unavailable or the report write fails, the assessment turn fails.

The assessment appears in Schedules even while disabled. Its Enable/Disable and Run Now buttons call the same assessment endpoints used by Agents → Assistant, and Edit opens the shared schedule modal with interval recurrence editing. The initial assessment result, individual proposals, Goal questions, and later follow-ups remain in that assessment's Assistant session and Telegram topic. If the Assistant bot is disconnected, the conversation remains available in the Agents UI and backend-authored messages await topic delivery.

Startup also reconciles checked-in installed skill packages before loading scheduler rows. The installed `daily_feed_service` has one required schedule, initially disabled, with a daily 08:30 `America/Toronto` default. It deterministically fetches the current day's Toronto forecast from the fixed [Open-Meteo forecast endpoint](https://open-meteo.com/en/docs), produces threshold-based clothing guidance, lists today's primary-calendar events through `google_calendar.event.list`, and groups incomplete Notion todos into overdue, today, tomorrow, and the day after tomorrow through `notion.todo.list`. Date-only todos become overdue after their due date; timed todos become overdue once their Toronto-local due time passes. It then replaces the separately configured standalone page through `notion.daily_feed.write`. The service has no Codex permission or LLM calls, accepts only `{}`, keeps no feed history or cache, and emits bounded concise Markdown. Open-Meteo access is limited to `api.open-meteo.com`; the fixed coordinates are Toronto (`43.6532`, `-79.3832`) and all date boundaries use `America/Toronto`. Runtime network and integration approvals plus the normal explicit service enable remain required before execution.

The installed `personal_weekly_summary_service` defaults to Sunday 21:00 `America/Toronto`, initially disabled. It reads Notion Todos, Atlas Goals, and primary Google Calendar events, calculates deterministic weekly metrics and four-week trends, and creates a `Personal Feed` Notion report without Codex. Its definitions, snapshot state, source limitations, and configurable active hours are documented in [Personal Weekly Summary](personal_weekly_summary.md).

The installed `weekly_report_service` has one required schedule, initially disabled, with a Monday 08:00 `America/Toronto` default. Its v2 package calls three user-owned functions and deterministically formats their structured output into separate native Notion reports:

- `github_repo_scout` receives repository full names from `seen_repositories.json` as `excluded_repo`. It retrieves up to 25 Trending repositories, excludes previously seen names, and considers at most the first 15 remaining unique candidates. Codex uses Atlas goals, projects, and interests as optional personalization context and may select zero to six repositories. The service creates a `GitHub Projects` report, then appends all returned `seen_repo` candidate names, including unselected candidates.
- `research_paper_scout` receives stable paper IDs from a separate `seen_papers.json`. The provider walks the current month's Hugging Face upvote ranking, skips seen IDs before applying the limit, and returns the next six unseen papers. The scout fetches their full content and makes one Codex call to analyze all of them without model selection or re-ranking. The service creates an `AI Research` report containing only each linked title, linked organization when available, upvote count, and concise plain-language analysis, then appends the analyzed IDs to `seen_papers.json`. See [Research Paper Scout](../skills/research_paper_scout.md).
- `macro_geopolitical_news_scout` receives the previous report, seen release IDs/URLs, and last successful fetch time from `macro_geopolitical_news_scout.json` in the service cache. It gathers official releases and configured macro data, checks optional Atlas context, and uses one internet-enabled Codex call for selective macro and geopolitical analysis. The service creates a `Macro` report and saves the new report, seen IDs/URLs, and fetch time only after report creation. See [Macro / Geopolitical News Scout](../skills/macro_geopolitical_news_scout.md).

All three history files live only in the service's own cache. Each advances only after its corresponding Notion report succeeds. Empty selections produce explicit empty reports. The service never calls Codex directly or asks a model to write Notion blocks. It validates the output and splits analysis text into bounded Notion rich-text chunks.

After all three reports succeed, `telegram.notification.send` sends `Your weekly reports are ready` with all three report names. A failed run attempts a `Weekly report failed` alert with the normalized error code and message; alert failure never replaces the original error. A later failure does not undo an earlier successful report or its persisted history. The new child function and effective permissions require the normal runtime review before execution. All nested work shares the existing service-run deadline; no automatic retries or extra schedules are added.

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
