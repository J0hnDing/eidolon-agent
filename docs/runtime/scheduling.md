# Scheduling

Scheduling uses APScheduler through `SchedulerService`.

## Schedule Types

Supported schedule forms:

- daily at HH:MM;
- weekly on day plus HH:MM;
- interval every N minutes/hours/days.

## Schedule Creation

Schedules can be created from the UI or from manifest-declared schedule intent. Skills do not register schedules by executing code. The app validates schedule metadata and asks the user for approval.

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
- skill type is automation or hybrid;
- runtime permissions are approved;
- runtime permissions are supported by the runner;
- schedule is active and approved.

Scheduled runs are stored in `skill_runs` and update schedule last/next run fields.
