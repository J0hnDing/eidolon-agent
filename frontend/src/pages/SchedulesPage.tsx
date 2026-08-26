import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { SkillSchedule, api } from "../api/client";
import { usePolling } from "../lib/usePolling";

export default function SchedulesPage() {
  const [schedules, setSchedules] = useState<SkillSchedule[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isWorking, setIsWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadSchedules();
  }, []);

  usePolling(() => loadSchedules({ showLoading: false }), schedules.some((schedule) => schedule.status === "active" || schedule.status === "pending"), 5000);

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
      await loadSchedules();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Schedule action failed");
    } finally {
      setIsWorking(false);
    }
  }

  return (
    <section className="page stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">Recurring runs</p>
          <h1>Schedules</h1>
          <p className="muted">Review platform jobs and manage approved installed-function schedules.</p>
        </div>
      </header>

      {error && <p className="error-text">{error}</p>}
      {isLoading ? (
        <p className="muted">Loading schedules...</p>
      ) : (
        <section className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Skill</th>
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
                    <strong>
                      {schedule.schedule_kind === "platform"
                        ? "Backend Core"
                        : schedule.skill_name ?? `Skill #${schedule.skill_id}`}
                    </strong>
                  </td>
                  <td>
                    <strong>{schedule.name}</strong>
                    <span className="table-subtitle">{humanSchedule(schedule)}</span>
                  </td>
                  <td>
                    <span className={`badge status-${schedule.status}`}>{schedule.status}</span>
                  </td>
                  <td>{formatTimestamp(schedule.next_run_at, "not scheduled")}</td>
                  <td>
                    {formatTimestamp(schedule.last_run_at, "never")}
                    {schedule.last_run_status && (
                      <span className={`badge run-${schedule.last_run_status}`}>{schedule.last_run_status}</span>
                    )}
                  </td>
                  <td>
                    {schedule.read_only ? (
                      <span className="muted">Managed by Eidolon</span>
                    ) : (
                      <div className="button-row">
                      {schedule.status === "pending" && (
                        <>
                          <button type="button" onClick={() => act(() => api.approveSchedule(schedule.id))} disabled={isWorking}>
                            Approve
                          </button>
                          <button type="button" className="secondary" onClick={() => act(() => api.denySchedule(schedule.id))} disabled={isWorking}>
                            Deny
                          </button>
                        </>
                      )}
                      {schedule.status === "active" && (
                        <button type="button" className="secondary" onClick={() => act(() => api.pauseSchedule(schedule.id))} disabled={isWorking}>
                          Pause
                        </button>
                      )}
                      {schedule.status === "paused" && (
                        <button type="button" onClick={() => act(() => api.resumeSchedule(schedule.id))} disabled={isWorking}>
                          Resume
                        </button>
                      )}
                      <button type="button" className="secondary" onClick={() => act(() => api.runScheduleNow(schedule.id))} disabled={isWorking || schedule.status !== "active"}>
                        Run Now
                      </button>
                      <button type="button" className="danger" onClick={() => act(() => api.deleteSchedule(schedule.id))} disabled={isWorking}>
                        Delete
                      </button>
                        {schedule.skill_id !== null && <Link to={`/skills/${schedule.skill_id}`}>Skill</Link>}
                      </div>
                    )}
                  </td>
                </tr>
              ))}
              {schedules.length === 0 && (
                <tr>
                  <td colSpan={6} className="muted">
                    No schedules yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </section>
      )}
    </section>
  );
}

function humanSchedule(schedule: SkillSchedule): string {
  const data = schedule.schedule_json;
  if (schedule.schedule_type === "daily") return `Daily at ${data.time} ${schedule.timezone}`;
  if (schedule.schedule_type === "weekly") return `Weekly on ${data.day} at ${data.time} ${schedule.timezone}`;
  if (schedule.schedule_type === "interval") return `Every ${data.every} ${data.unit}`;
  return schedule.schedule_type;
}

function formatTimestamp(value: string | null, fallback: string): string {
  if (!value) return fallback;
  const hasTimezone = /(?:z|[+-]\d{2}:?\d{2})$/i.test(value);
  const normalized = hasTimezone ? value : `${value}Z`;
  return new Date(normalized).toLocaleString();
}
