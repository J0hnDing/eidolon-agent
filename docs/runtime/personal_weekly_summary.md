# Personal Weekly Summary Service

`personal_weekly_summary_service` is a checked-in installed service. Its required schedule defaults to Sunday 21:00 `America/Toronto`, initially disabled under the ordinary installed-service approval and Enable flow. The run has no Codex/LLM permission. It reads `notion.todo.list`, `atlas.goal.list`, and `google_calendar.event.list`, then creates one Notion report with `Select = Personal Feed`. It accepts optional `active_start` and `active_end` schedule input in `HH:MM` form; defaults are 09:00 and 24:00 on Monday through Friday. The end must follow the start on the same civil day.

The named week is Monday 00:00 through the following Monday 00:00 in Toronto. The Sunday 21:00 run observes Todo and Goal state at run time. It counts the full named week's scheduled Calendar events, including events planned for the final three hours after the run. A manual run earlier in the week still uses that week's boundaries and is marked already reported on subsequent runs in the same week.

## Metric definitions

| Metric | Definition |
| --- | --- |
| Completion rate | Completed cohort todos / cohort todos. The cohort is IDs open and not Archived at the preceding Sunday snapshot plus todos created from Monday 00:00 through this run. Missing, manually trashed, or manually Archived without Done IDs are excluded because their final state is unknowable. `Done` at this run defines completion. |
| Observed on-time completion rate | Among completed cohort todos with a Due At value, the share whose Notion `last_edited_at` is no later than the due instant. A date-only due value ends at the next local midnight. This is a conservative observation proxy, not an exact completion timestamp: later edits and the daily archive operation can move it later. |
| Carryover rate | Cohort todos still not Done at this run / cohort todos. |
| Goals advanced this week | Previously observed flat Atlas Goals whose assessed blueprint-derived progress increased from the preceding Sunday snapshot. Both observations must have finite numeric progress; new or unassessed Goals are not inferred to have advanced. |
| Goal coverage rate | Advanced Goals / previously assessed Goals below 100% that still have assessed progress. A Goal reaching 100% this week remains in that starting cohort; new Goals enter after their first snapshot. |
| Weeks since last advancement | Full weekly snapshot intervals since the last observed progress increase for each current assessed Goal below 100%. Until an increase is observed, the value is unknown. The last observed advancement date is retained while that Goal remains present. |
| New goals and blueprint steps | New Goal IDs and new blueprint step IDs (scoped to their owning Goal) relative to the preceding Sunday snapshot. Blueprint steps are not separate Goal records. |
| Total scheduled hours | Duration of the union of timed, confirmed or tentative primary-calendar event intervals clipped to the Monday-to-Monday week. Overlaps count once. All-day events are excluded. |
| Calendar occupation rate | Duration of merged timed events inside Monday–Friday 09:00–24:00 Toronto / 75 hours. This fixed window does not change with the configurable active hours. |
| Longest free block | The largest continuous uncovered interval within any single weekday's configured active hours. It does not join gaps across days. |

Rates with a zero denominator show N/A. On the first run, Todo and Goal comparison metrics show N/A while the service establishes the first snapshot. A pre-progress Goal snapshot also establishes a new progress baseline. If the prior successful snapshot is not from the immediately preceding week, comparison metrics establish a new baseline instead of treating a multiweek change as one week's work. Calendar metrics remain available.

The service stores only a current set of open Todo IDs, a flat Goal snapshot with owner-scoped blueprint step identities, last observed Goal advancement dates, the report ID, and four weekly metric rows in its own `./cache/personal_weekly_summary.json`. It does not store Todo titles or full calendar events. The report shows each metric's change from the preceding stored week and a compact four-observation sparkline, with exact values beside it. State advances only after Notion confirms report creation; a run repeated during the same week returns the saved report ID. Notion report creation and the local cache write are not one transaction, so a cache-write failure after Notion succeeds can still leave an untracked report.

The semantic Goal list returns at most 100 flat Goals; the general Atlas record search provides pagination for broader reads. Atlas Goals have a separate pursuit status; this summary's starting progress cohort requires assessed progress below 100%. Nullable and fractional progress are preserved, and unassessed progress is shown explicitly. Legacy cached Goal trees establish a fresh progress/blueprint baseline while Todo and Calendar history is retained. Exact Todo completion time still requires a new source timestamp; the report labels its last-edit proxy instead of presenting it as an exact measurement.
