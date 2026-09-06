import { FormEvent, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { SchedulePayload, ScheduleType, SkillSchedule, api } from "../api/client";
import { RunningStatus } from "../components/RunningStateDot";
import { parseBackendDateTime } from "../lib/dateTime";
import { formatDisplayName } from "../lib/displayName";
import { usePolling } from "../lib/usePolling";

export default function SchedulesPage() {
  const [schedules, setSchedules] = useState<SkillSchedule[]>([]);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isWorking, setIsWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadSchedules();
  }, []);

  usePolling(
    () => loadSchedules({ showLoading: false }),
    schedules.some((schedule) => schedule.schedule_kind === "platform"
      ? schedule.status === "active"
      : schedule.skill_enabled === true),
    5000,
  );

  async function loadSchedules(options: { showLoading?: boolean } = {}) {
    if (options.showLoading !== false) setIsLoading(true);
    setError(null);
    try {
      setSchedules(await api.listSchedules());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load schedules");
    } finally {
      if (options.showLoading !== false) setIsLoading(false);
    }
  }

  async function act(action: () => Promise<unknown>) {
    setIsWorking(true);
    setError(null);
    try {
      await action();
      await loadSchedules({ showLoading: false });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Schedule action failed");
    } finally {
      setIsWorking(false);
    }
  }

  const editing = schedules.find((schedule) => schedule.id === editingId) ?? null;

  function isEnabled(schedule: SkillSchedule): boolean {
    return schedule.schedule_kind === "platform"
      ? schedule.status === "active"
      : schedule.skill_enabled === true;
  }

  async function toggleSchedule(schedule: SkillSchedule) {
    const enabled = isEnabled(schedule);
    if (schedule.service_id === "backend.assistant.assessment") {
      await act(() => api.updateAssistantAssessment(!enabled));
    } else if (schedule.schedule_kind === "platform") {
      await act(() => api.updatePlatformScheduleAvailability(schedule.service_id!, !enabled));
    } else {
      await act(() => api.updateSkill(schedule.skill_id!, { enabled: !enabled }));
    }
  }

  async function runNow(schedule: SkillSchedule) {
    if (schedule.service_id === "backend.assistant.assessment") {
      await act(() => api.runAssistantAssessment());
    } else if (schedule.schedule_kind === "platform") {
      await act(() => api.runPlatformScheduleNow(schedule.service_id!));
    } else {
      await act(() => api.runScheduleNow(schedule.id));
    }
  }

  return (
    <section className="page stack">
      <header className="page-header">
        <div>
          <h1>Schedules</h1>
          <p className="muted">Manage generated service schedules and review backend-owned service jobs.</p>
        </div>
      </header>

      {error && <p className="error-text">{error}</p>}
      {isLoading ? (
        <p className="muted">Loading schedules...</p>
      ) : (
        <section className="table-wrap">
          <table className="schedules-table">
            <colgroup>
              <col className="schedule-column-service" />
              <col className="schedule-column-recurrence" />
              <col className="schedule-column-status" />
              <col className="schedule-column-next-run" />
              <col className="schedule-column-last-run" />
              <col className="schedule-column-actions" />
            </colgroup>
            <thead>
              <tr>
                <th>Service</th>
                <th>Schedule</th>
                <th>Status</th>
                <th>Next Run</th>
                <th>Last Run</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {schedules.map((schedule) => (
                <tr key={`${schedule.schedule_kind}-${schedule.id}`}>
                  <td>
                    <span className="entry-title-line">
                      <strong>
                        {schedule.skill_id !== null ? (
                          <Link className="table-title-link reveal-arrow-link" to={`/skills/${schedule.skill_id}`}>
                            {formatDisplayName(schedule.skill_name ?? `Service #${schedule.skill_id}`)}
                            <span className="row-reveal-arrow" aria-hidden="true">→</span>
                          </Link>
                        ) : formatDisplayName(schedule.name)}
                      </strong>
                    </span>
                  </td>
                  <td>{humanSchedule(schedule)}</td>
                  <td>
                    <span className="table-status-stack">
                      <span className={`badge status-${isEnabled(schedule) ? "active" : "paused"}`}>
                        {isEnabled(schedule) ? "active" : "paused"}
                      </span>
                      {schedule.is_running && <RunningStatus />}
                    </span>
                  </td>
                  <td>{formatTimestamp(schedule.next_run_at, "not scheduled")}</td>
                  <td>
                    <span className="schedule-last-run">
                      {schedule.last_run_status && (
                        <span
                          className={`status-dot run-${schedule.last_run_status}`}
                          aria-label={`Last run status: ${schedule.last_run_status}`}
                          title={schedule.last_run_status}
                        />
                      )}
                      {formatTimestamp(schedule.last_run_at, "never")}
                    </span>
                  </td>
                  <td>
                    <div className="button-row schedule-actions">
                      <button
                        type="button"
                        className={`${isEnabled(schedule) ? "secondary " : ""}schedule-state-action`}
                        onClick={() => toggleSchedule(schedule)}
                        disabled={isWorking}
                      >
                        {isEnabled(schedule) ? "Disable" : "Enable"}
                      </button>
                      <button
                        type="button"
                        className="secondary"
                        onClick={() => runNow(schedule)}
                        disabled={isWorking || (schedule.service_id !== "backend.assistant.assessment" && !isEnabled(schedule))}
                      >
                        Run Now
                      </button>
                      {schedule.service_id === "backend.assistant.assessment" ? (
                        <Link className="button-link secondary" to="/agents/assistant">Edit</Link>
                      ) : (
                        <button type="button" className="secondary" onClick={() => setEditingId(schedule.id)} disabled={isWorking}>
                          Edit
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
              {schedules.length === 0 && (
                <tr><td colSpan={6} className="muted">No service schedules yet.</td></tr>
              )}
            </tbody>
          </table>
        </section>
      )}

      {editing && (
        <ScheduleEditor
          key={editing.id}
          schedule={editing}
          inputEditable={editing.schedule_kind === "service"}
          isWorking={isWorking}
          onCancel={() => setEditingId(null)}
          onSave={(payload) => act(async () => {
            if (editing.schedule_kind === "platform") {
              await api.updatePlatformSchedule(editing.service_id!, payload);
            } else {
              await api.updateSchedule(editing.id, payload);
            }
            setEditingId(null);
          })}
        />
      )}
    </section>
  );
}

function ScheduleEditor({ schedule, inputEditable, isWorking, onCancel, onSave }: {
  schedule: SkillSchedule;
  inputEditable: boolean;
  isWorking: boolean;
  onCancel: () => void;
  onSave: (payload: { name: string; schedule: SchedulePayload }) => Promise<void>;
}) {
  const data = schedule.schedule_json;
  const [name, setName] = useState(schedule.name);
  const [type, setType] = useState<ScheduleType>(schedule.schedule_type);
  const [time, setTime] = useState(data.time ?? "08:00");
  const [day, setDay] = useState(data.day ?? "monday");
  const [every, setEvery] = useState(data.every ?? 60);
  const [unit, setUnit] = useState<"minutes" | "hours" | "days">(data.unit ?? "minutes");
  const [timezone, setTimezone] = useState(schedule.timezone);
  const [input, setInput] = useState(JSON.stringify(schedule.input_json, null, 2));
  const [inputError, setInputError] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      const parsed = JSON.parse(input) as unknown;
      if (parsed === null || Array.isArray(parsed) || typeof parsed !== "object") {
        throw new Error("Input must be a JSON object");
      }
      setInputError(null);
      await onSave({
        name,
        schedule: {
          type,
          timezone,
          input: parsed as Record<string, unknown>,
          ...(type === "daily" ? { time } : {}),
          ...(type === "weekly" ? { day, time } : {}),
          ...(type === "interval" ? { every, unit } : {}),
        },
      });
    } catch (err) {
      setInputError(err instanceof Error ? err.message : "Input must be valid JSON");
    }
  }

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="schedule-editor-title">
      <form className="modal-panel schedule-editor-modal stack" onSubmit={submit}>
        <header className="page-header">
          <div>
            <h2 id="schedule-editor-title">Edit Service Schedule</h2>
            <p className="muted">Changes are runtime state and remain in place across service version updates.</p>
          </div>
        </header>
        <div className="form-grid">
          <label>Name<input value={name} onChange={(event) => setName(event.target.value)} required /></label>
          <label>
            Type
            <select value={type} onChange={(event) => setType(event.target.value as ScheduleType)}>
              <option value="daily">daily</option>
              <option value="weekly">weekly</option>
              <option value="interval">interval</option>
            </select>
          </label>
          {(type === "daily" || type === "weekly") && (
            <label>Time<input type="time" value={time} onChange={(event) => setTime(event.target.value)} required /></label>
          )}
          {type === "weekly" && (
            <label>
              Day
              <select value={day} onChange={(event) => setDay(event.target.value)}>
                {["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"].map((value) => (
                  <option key={value} value={value}>{value}</option>
                ))}
              </select>
            </label>
          )}
          {type === "interval" && (
            <>
              <label>Every<input type="number" min="1" value={every} onChange={(event) => setEvery(Number(event.target.value))} required /></label>
              <label>
                Unit
                <select value={unit} onChange={(event) => setUnit(event.target.value as "minutes" | "hours" | "days")}>
                  <option value="minutes">minutes</option>
                  <option value="hours">hours</option>
                  <option value="days">days</option>
                </select>
              </label>
            </>
          )}
          <label>Timezone<input value={timezone} onChange={(event) => setTimezone(event.target.value)} required /></label>
        </div>
        {inputEditable && <label>Input JSON<textarea rows={7} value={input} onChange={(event) => setInput(event.target.value)} /></label>}
        {inputError && <p className="error-text">{inputError}</p>}
        <div className="button-row">
          <button type="submit" disabled={isWorking}>Save Schedule</button>
          <button type="button" className="secondary" onClick={onCancel} disabled={isWorking}>Cancel</button>
        </div>
      </form>
    </div>
  );
}

function humanSchedule(schedule: SkillSchedule): string {
  const data = schedule.schedule_json;
  if (schedule.schedule_type === "daily") return `Daily at ${data.time}`;
  if (schedule.schedule_type === "weekly") return `Weekly on ${capitalize(data.day)} at ${data.time}`;
  if (schedule.schedule_type === "interval") return `Every ${data.every} ${data.unit}`;
  return schedule.schedule_type;
}

function capitalize(value: string | null | undefined): string {
  return value ? `${value.charAt(0).toUpperCase()}${value.slice(1)}` : "";
}

function formatTimestamp(value: string | null, fallback: string): string {
  if (!value) return fallback;
  const date = parseBackendDateTime(value);
  if (Number.isNaN(date.getTime())) return fallback;
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}
