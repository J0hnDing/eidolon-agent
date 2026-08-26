# Scheduling

Scheduling uses APScheduler through `SchedulerService`.

In addition to user-approved `SkillSchedule` jobs, `SchedulerService` registers the platform-owned `backend.notion.todo.cleanup_done` backend-core function every time the backend starts. It runs daily at 03:00 `America/Toronto`, uses a stable replacement job id, coalesces missed runs, and permits only one concurrent instance. `GET /schedules` includes a read-only platform-schedule projection with its next and last run state; it is not a mutable `SkillSchedule` row and remains outside schedule approval and mutation flows.

## Schedule Types

Supported schedule forms:

- daily at HH:MM;
- weekly on day plus HH:MM;
- interval every N minutes/hours/days.

## Schedule Creation

Schedules can be created for `function` skills from the UI or from manifest-declared schedule intent. Skills do not register schedules by executing code. BuilderAgent does not receive a Scheduling API. ProductManager expresses recurring function intent in the manifest schedule field, Builder preserves that field, and the backend registers it during install. `web_app` manifests reject schedules because a persistent service is not a bounded scheduled run.

Manifest-declared schedules are created as pending schedule records, even if a skill is installed disabled by default. The schedule does not become active until approved, and scheduled execution still requires the skill to be enabled.

Schedule records use four statuses: `pending`, `active`, `paused`, and `denied`. Deleting a schedule unregisters it and removes its database row rather than assigning a `deleted` status.

## Approval

Schedule approval shows:

- skill name;
- when it will run;
- input JSON;
- permissions required by the skill;
- reminder that schedule approval does not bypass runtime approval.

## Execution Checks

A scheduled run may execute only if:

- skill is installed;
- skill runtime is `function`;
- skill is enabled;
- runtime permissions are approved;
- runtime permissions are supported by the runner;
- schedule is active and approved.

Scheduled runs are stored in `skill_runs` and update schedule last/next run fields. A skill that returns top-level `status: "partial"` or `status: "failed"` records that outcome instead of being marked succeeded merely because its process exited with code zero and emitted valid JSON.
