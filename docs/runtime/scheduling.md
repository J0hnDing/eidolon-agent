# Scheduling

Scheduling uses APScheduler through `SchedulerService`.

## Schedule Types

Supported schedule forms:

- daily at HH:MM;
- weekly on day plus HH:MM;
- interval every N minutes/hours/days.

## Schedule Creation

Schedules can be created from the UI or from manifest-declared schedule intent. Skills do not register schedules by executing code. BuilderAgent does not receive a Scheduling API. ProductManager expresses recurring intent in the manifest schedule field, Builder preserves that field, and the backend registers it during install.

Manifest-declared schedules are created as pending schedule records, even if an automation skill is installed disabled by default. The schedule does not become active until approved, and scheduled execution still requires the skill to be enabled.

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
- skill is enabled;
- skill type is automation;
- runtime permissions are approved;
- runtime permissions are supported by the runner;
- schedule is active and approved.

Scheduled runs are stored in `skill_runs` and update schedule last/next run fields.
