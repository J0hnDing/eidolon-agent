import { FormEvent, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { SchedulePayload, ScheduleType, SkillSchedule, api } from "../api/client";
import { AppIcon } from "../components/AppIcon";
import { Modal } from "../components/Modal";
import { RunningStatus } from "../components/RunningStateDot";
import { parseBackendDateTime } from "../lib/dateTime";
import { formatDisplayName } from "../lib/displayName";
import { usePolling } from "../lib/usePolling";
import "./catalog.css";

export default function SchedulesPage() {
  const [schedules, setSchedules] = useState<SkillSchedule[]>([]);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isWorking, setIsWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [keyword, setKeyword] = useState("");
  const [kind, setKind] = useState("all");
  const [availability, setAvailability] = useState("all");

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

  const filteredSchedules = schedules.filter((schedule) =>
    (kind === "all" || schedule.schedule_kind === kind)
    && (availability === "all" || (availability === "active" ? isEnabled(schedule) : !isEnabled(schedule)))
    && `${schedule.name} ${schedule.skill_name ?? ""} ${formatDisplayName(schedule.skill_name ?? schedule.name)} ${humanSchedule(schedule)}`.toLocaleLowerCase().includes(keyword.trim().toLocaleLowerCase()),
  );

  async function toggleSchedule(schedule: SkillSchedule) {
    const enabled = isEnabled(schedule);
    if (schedule.schedule_kind === "platform") {
      await act(() => api.updatePlatformScheduleAvailability(schedule.service_id!, !enabled));
    } else {
      await act(() => api.updateSkill(schedule.skill_id!, { enabled: !enabled }));
    }
  }

  async function runNow(schedule: SkillSchedule) {
    if (schedule.schedule_kind === "platform") {
      await act(() => api.runPlatformScheduleNow(schedule.service_id!));
    } else {
      await act(() => api.runScheduleNow(schedule.id));
    }
  }

  return (
    <section className="page stack catalog-page automations-page">
      <header className="page-header">
        <div>
          <h1>Automations</h1>
        </div>
        <button type="button" className="secondary" onClick={() => void loadSchedules()} disabled={isLoading}>
          <AppIcon name="refresh" size={15} />Refresh
        </button>
      </header>

      <div className="catalog-tabs" aria-label="Automation type filters">
        {[["all", "All automations"], ["service", "Skill automations"], ["platform", "System automations"]].map(([value, label]) => (
          <button className={kind === value ? "catalog-tab is-active" : "catalog-tab"} type="button" key={value} aria-pressed={kind === value} onClick={() => setKind(value)}>{label}<span>{value === "all" ? schedules.length : schedules.filter((schedule) => schedule.schedule_kind === value).length}</span></button>
        ))}
      </div>
      <div className="catalog-toolbar">
        <label className="catalog-search"><AppIcon name="search" size={17} /><input aria-label="Search automations" type="search" placeholder="Search automations…" value={keyword} onChange={(event) => setKeyword(event.target.value)} /></label>
        <label className="catalog-select"><span>Status</span><select aria-label="Status" value={availability} onChange={(event) => setAvailability(event.target.value)}><option value="all">All statuses</option><option value="active">Active</option><option value="paused">Paused</option></select></label>
        <span className="catalog-result-count" aria-live="polite">{filteredSchedules.length} {filteredSchedules.length === 1 ? "automation" : "automations"}</span>
      </div>

      {error && <p className="error-text">{error}</p>}
      {isLoading ? (
        <p className="muted">Loading automations...</p>
      ) : (
        schedules.length === 0 ? <div className="catalog-empty"><div className="catalog-empty-icon"><AppIcon name="clock" size={30} /></div><h2>Make room for recurring work</h2><p>No automations are scheduled yet.</p><p>Install a service skill to add its schedule, then enable it when you’re ready.</p><Link className="button-link secondary" to="/skills">Browse skills<AppIcon name="arrow" size={15} /></Link></div>
        : filteredSchedules.length === 0 ? <div className="catalog-empty"><AppIcon name="search" size={28} /><h2>No automations match these filters</h2><p>Try another search or show all automations.</p><button type="button" className="secondary" onClick={() => { setKeyword(""); setKind("all"); setAvailability("all"); }}>Clear filters</button></div>
        : <section className="catalog-list automation-list">
        <div className="table-wrap">
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
                <th>Automation</th>
                <th>Recurrence</th>
                <th>Status</th>
                <th>Next run</th>
                <th>Last run</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {filteredSchedules.map((schedule) => (
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
                    <span className="table-subtitle">{schedule.schedule_kind === "platform" ? "System automation" : "Skill automation"}</span>
                  </td>
                  <td title={`Schedule timezone: ${schedule.timezone}`}>{humanSchedule(schedule)}</td>
                  <td>
                    {schedule.is_running ? (
                      <RunningStatus />
                    ) : (
                      <span className={`badge status-${isEnabled(schedule) ? "active" : "paused"}`}>
                        {isEnabled(schedule) ? "active" : "paused"}
                      </span>
                    )}
                  </td>
                  <td><span className="catalog-time">{formatTimestamp(schedule.next_run_at, "not scheduled")}</span></td>
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
                        disabled={isWorking || !isEnabled(schedule)}
                      >
                        Run Now
                      </button>
                      <button type="button" className="secondary" onClick={() => setEditingId(schedule.id)} disabled={isWorking}>
                        Edit
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="catalog-table-note">Run times use your local timezone. Hover a recurrence to see its schedule timezone.</p>
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
    <Modal title="Edit automation" className="schedule-editor-modal catalog-editor" onClose={() => { if (!isWorking) onCancel(); }}>
      <form className="stack" onSubmit={submit}>
        <p className="muted">Choose when this automation runs. Changes stay in place across service updates.</p>
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
        {inputEditable && <details className="catalog-advanced"><summary>Advanced input</summary><label>Input JSON<textarea rows={7} value={input} onChange={(event) => setInput(event.target.value)} /></label></details>}
        {inputError && <p className="error-text">{inputError}</p>}
        <div className="button-row">
          <button type="submit" disabled={isWorking}>Save automation</button>
          <button type="button" className="secondary" onClick={onCancel} disabled={isWorking}>Cancel</button>
        </div>
      </form>
    </Modal>
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
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}
