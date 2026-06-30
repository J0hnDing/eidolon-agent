import { FormEvent, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import PermissionRequestModal from "../components/PermissionRequestModal";
import {
  AgentRun,
  ApprovalRequest,
  ProposedSkillValidation,
  RunnerStatus,
  ScheduleType,
  Skill,
  SkillFile,
  SkillRun,
  SkillSchedule,
  api,
} from "../api/client";

export default function SkillDetailPage() {
  const { skillId } = useParams();
  const navigate = useNavigate();
  const [skill, setSkill] = useState<Skill | null>(null);
  const [runs, setRuns] = useState<SkillRun[]>([]);
  const [files, setFiles] = useState<SkillFile[]>([]);
  const [validation, setValidation] = useState<ProposedSkillValidation | null>(null);
  const [runtimePermission, setRuntimePermission] = useState<ApprovalRequest | null>(null);
  const [runnerStatus, setRunnerStatus] = useState<RunnerStatus | null>(null);
  const [schedules, setSchedules] = useState<SkillSchedule[]>([]);
  const [agentRuns, setAgentRuns] = useState<AgentRun[]>([]);
  const [showRuntimeModal, setShowRuntimeModal] = useState(false);
  const [runInput, setRunInput] = useState('{\n  "hello": "world"\n}');
  const [scheduleName, setScheduleName] = useState("Daily run");
  const [scheduleType, setScheduleType] = useState<ScheduleType>("daily");
  const [scheduleTime, setScheduleTime] = useState("08:00");
  const [scheduleDay, setScheduleDay] = useState("monday");
  const [scheduleEvery, setScheduleEvery] = useState(60);
  const [scheduleUnit, setScheduleUnit] = useState<"minutes" | "hours" | "days">("minutes");
  const [scheduleTimezone, setScheduleTimezone] = useState("America/Toronto");
  const [scheduleInput, setScheduleInput] = useState("{}");
  const [isLoading, setIsLoading] = useState(true);
  const [isRunning, setIsRunning] = useState(false);
  const [isWorking, setIsWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const latestRun = runs[0] ?? null;

  useEffect(() => {
    async function loadSkillDetail() {
      if (!skillId) return;
      setIsLoading(true);
      setError(null);
      try {
        const id = Number(skillId);
        const [loadedSkill, loadedRuns, loadedFiles, permissionRequests, loadedRunnerStatus, loadedSchedules, loadedAgentRuns] = await Promise.all([
          api.getSkill(id),
          api.listSkillRuns(id),
          api.listSkillFiles(id),
          api.listPermissionRequests({ skill_id: id, request_scope: "runtime" }),
          api.getRunnerStatus(),
          api.listSchedules(id),
          api.listAgentRuns(),
        ]);
        setSkill(loadedSkill);
        setRuns(loadedRuns);
        setFiles(loadedFiles);
        setRuntimePermission(permissionRequests[0] ?? null);
        setRunnerStatus(loadedRunnerStatus);
        setSchedules(loadedSchedules);
        setAgentRuns(loadedAgentRuns.filter((run) => run.skill_id === id));
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not load skill");
      } finally {
        setIsLoading(false);
      }
    }
    loadSkillDetail();
  }, [skillId]);

  async function handleRun() {
    if (!skill) return;
    if (runtimePermission?.status !== "approved") {
      await handleReviewRuntimePermissions(true);
      return;
    }
    setIsRunning(true);
    setError(null);
    try {
      const parsedInput = parseRunInput(runInput);
      if (parsedInput === null || Array.isArray(parsedInput) || typeof parsedInput !== "object") {
        throw new Error("Run input must be a JSON object");
      }
      const run = await api.runSkill(skill.id, parsedInput as Record<string, unknown>);
      setRuns((current) => [run, ...current.filter((item) => item.id !== run.id)]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not run skill");
    } finally {
      setIsRunning(false);
    }
  }

  async function handleValidate() {
    if (!skill) return;
    setIsWorking(true);
    setError(null);
    try {
      setValidation(await api.validateSkill(skill.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not validate skill");
    } finally {
      setIsWorking(false);
    }
  }

  async function handleInstall() {
    if (!skill) return;
    if (runtimePermission?.status !== "approved") {
      await handleReviewRuntimePermissions(true);
      return;
    }
    setIsWorking(true);
    setError(null);
    try {
      const installed = await api.installSkill(skill.id);
      setSkill(installed);
      setValidation(null);
      setFiles(await api.listSkillFiles(installed.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not install skill");
    } finally {
      setIsWorking(false);
    }
  }

  async function handleReject() {
    if (!skill) return;
    setIsWorking(true);
    setError(null);
    try {
      await api.rejectSkill(skill.id);
      navigate("/skills");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not reject skill");
    } finally {
      setIsWorking(false);
    }
  }

  async function handleDelete() {
    if (!skill) return;
    setIsWorking(true);
    setError(null);
    try {
      await api.deleteSkill(skill.id);
      navigate("/skills");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not delete skill");
    } finally {
      setIsWorking(false);
    }
  }

  async function handleRepair() {
    if (!skill) return;
    setIsWorking(true);
    setError(null);
    try {
      const agentRun = await api.repairSkill(skill.id);
      setAgentRuns((current) => [agentRun, ...current.filter((run) => run.id !== agentRun.id)]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start repair agent run");
    } finally {
      setIsWorking(false);
    }
  }

  async function handleReviewRuntimePermissions(openModal = true) {
    if (!skill) return;
    setIsWorking(true);
    setError(null);
    try {
      const request = await api.analyzeRuntimePermissions(skill.id);
      setRuntimePermission(request);
      setShowRuntimeModal(openModal);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not analyze runtime permissions");
    } finally {
      setIsWorking(false);
    }
  }

  async function handleApproveRuntimePermissions() {
    if (!skill) return;
    setIsWorking(true);
    setError(null);
    try {
      const request = await api.approveRuntimePermissions(skill.id);
      setRuntimePermission(request);
      setShowRuntimeModal(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not approve runtime permissions");
    } finally {
      setIsWorking(false);
    }
  }

  async function handleDenyRuntimePermissions() {
    if (!skill) return;
    setIsWorking(true);
    setError(null);
    try {
      const request = await api.denyRuntimePermissions(skill.id);
      setRuntimePermission(request);
      setShowRuntimeModal(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not deny runtime permissions");
    } finally {
      setIsWorking(false);
    }
  }

  async function handleToggleEnabled() {
    if (!skill) return;
    setIsWorking(true);
    setError(null);
    try {
      setSkill(await api.updateSkill(skill.id, { enabled: !skill.enabled }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not update skill");
    } finally {
      setIsWorking(false);
    }
  }

  async function refreshSchedules() {
    if (!skill) return;
    setSchedules(await api.listSchedules(skill.id));
  }

  async function handleCreateSchedule(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!skill) return;
    setIsWorking(true);
    setError(null);
    try {
      const input = parseRunInput(scheduleInput);
      const schedule = {
        type: scheduleType,
        timezone: scheduleTimezone,
        input,
        ...(scheduleType === "daily" ? { time: scheduleTime } : {}),
        ...(scheduleType === "weekly" ? { day: scheduleDay, time: scheduleTime } : {}),
        ...(scheduleType === "interval" ? { every: scheduleEvery, unit: scheduleUnit } : {}),
      };
      await api.createSchedule(skill.id, { name: scheduleName, schedule });
      await refreshSchedules();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create schedule");
    } finally {
      setIsWorking(false);
    }
  }

  async function handleCreateManifestSchedule() {
    if (!skill) return;
    setIsWorking(true);
    setError(null);
    try {
      await api.createManifestSchedule(skill.id);
      await refreshSchedules();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create schedule from manifest");
    } finally {
      setIsWorking(false);
    }
  }

  async function handleScheduleAction(action: () => Promise<unknown>) {
    setIsWorking(true);
    setError(null);
    try {
      await action();
      await refreshSchedules();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Schedule action failed");
    } finally {
      setIsWorking(false);
    }
  }

  if (isLoading) {
    return <p className="muted">Loading skill...</p>;
  }

  if (error || !skill) {
    return (
      <section className="page stack">
        <Link to="/skills">Back to skills</Link>
        <p className="error-text">{error ?? "Skill not found"}</p>
      </section>
    );
  }

  const isExecutable = skill.skill_type === "automation" || skill.skill_type === "hybrid";
  const isInstalledExecutable = skill.status === "installed" && isExecutable;
  const runtimeApproved = runtimePermission?.status === "approved";
  const canRun = isInstalledExecutable && skill.enabled && runtimeApproved;
  const isProposed = skill.status === "proposed";

  return (
    <section className="page stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">Skill Detail</p>
          <h1>{skill.name}</h1>
        </div>
        <Link to="/skills">Back to skills</Link>
      </header>

      <section className="detail-panel">
        <p>{skill.description}</p>
        <dl className="detail-grid">
          <div>
            <dt>Skill Type</dt>
            <dd>{skill.skill_type}</dd>
          </div>
          <div>
            <dt>Interface</dt>
            <dd>{skill.interface_type}</dd>
          </div>
          <div>
            <dt>Status</dt>
            <dd>{skill.status}</dd>
          </div>
          <div>
            <dt>Risk Level</dt>
            <dd>{skill.risk_level}</dd>
          </div>
          <div>
            <dt>Enabled</dt>
            <dd>{skill.enabled ? "enabled" : "disabled"}</dd>
          </div>
          <div>
            <dt>Manifest Path</dt>
            <dd>{skill.manifest_path}</dd>
          </div>
          <div>
            <dt>Instructions Path</dt>
            <dd>{skill.instructions_path ?? "none"}</dd>
          </div>
          <div>
            <dt>Installed Path</dt>
            <dd>{skill.installed_path ?? "not installed"}</dd>
          </div>
          <div>
            <dt>Input Schema</dt>
            <dd>{skill.input_schema_json ? "declared" : "none"}</dd>
          </div>
          <div>
            <dt>Output Schema</dt>
            <dd>{skill.output_schema_json ? "declared" : "none"}</dd>
          </div>
        </dl>
      </section>

      <section className="detail-panel">
        <header className="page-header">
          <div>
            <h2>Runtime Permissions</h2>
            <p className="muted">
              {runtimePermission
                ? runtimePermission.user_explanation || runtimePermission.reason
                : "Runtime permissions have not been analyzed yet."}
            </p>
          </div>
          <span className={`badge ${runtimePermission ? `risk-${runtimePermission.risk_level}` : ""}`}>
            {runtimePermission ? runtimePermission.status : "not analyzed"}
          </span>
        </header>
        {Boolean(runtimePermission?.reason_json?.permission_expansion) && (
          <p className="error-text">Permission expansion detected. Review the generated manifest before approving.</p>
        )}
        <div className="button-row">
          <button type="button" className="secondary" onClick={() => handleReviewRuntimePermissions(true)} disabled={isWorking}>
            Review Runtime Permissions
          </button>
        </div>
      </section>

      <section className="detail-panel">
        <header className="page-header">
          <div>
            <h2>Runner / Sandbox</h2>
            <p className="muted">
              {runnerStatus?.detail ?? "Runner status is not available."}
            </p>
          </div>
          <span className={`badge ${runnerStatus?.available ? "run-succeeded" : "run-blocked"}`}>
            {runnerStatus?.available ? "available" : "blocked"}
          </span>
        </header>
        <dl className="detail-grid">
          <div>
            <dt>Configured Mode</dt>
            <dd>{runnerStatus?.mode ?? "unknown"}</dd>
          </div>
          <div>
            <dt>Selected Runner</dt>
            <dd>{runnerStatus?.selected_mode ?? "unknown"}</dd>
          </div>
          <div>
            <dt>Docker</dt>
            <dd>{runnerStatus?.docker_available ? "available" : "unavailable"}</dd>
          </div>
          <div>
            <dt>Image</dt>
            <dd>{runnerStatus?.image ?? "not applicable"}</dd>
          </div>
          <div>
            <dt>Image Status</dt>
            <dd>{runnerStatus?.image_status ?? "unknown"}</dd>
          </div>
          <div>
            <dt>Sandbox Status</dt>
            <dd>
              {runnerStatus?.selected_mode === "docker" && runnerStatus.available
                ? "Docker sandbox active"
                : runnerStatus?.selected_mode === "local"
                  ? "Local dev runner"
                  : "Docker sandbox unavailable"}
            </dd>
          </div>
        </dl>
        {runnerStatus?.image_detail && <p className="muted">{runnerStatus.image_detail}</p>}
        {runnerStatus?.image_error && (
          <div className="run-detail">
            <h3>Image Build Error</h3>
            <pre>{runnerStatus.image_error}</pre>
          </div>
        )}
        {runnerStatus?.image_build_log && (
          <details className="run-detail">
            <summary>Image Build Log</summary>
            <pre>{runnerStatus.image_build_log}</pre>
          </details>
        )}
      </section>

      {isProposed ? (
        <div className="button-row">
          <button type="button" onClick={handleValidate} disabled={isWorking}>
            Validate/Test
          </button>
          <button type="button" onClick={handleInstall} disabled={isWorking}>
            Install
          </button>
          <button type="button" className="danger" onClick={handleReject} disabled={isWorking}>
            Reject/Delete
          </button>
          <button type="button" className="secondary" onClick={handleRepair} disabled={isWorking}>
            Ask Agents to Repair
          </button>
        </div>
      ) : (
        <div className="button-row">
          {canRun && (
            <button type="button" onClick={handleRun} disabled={isRunning}>
              {isRunning ? "Running..." : "Run"}
            </button>
          )}
          {isInstalledExecutable && skill.enabled && !runtimeApproved && (
            <button type="button" onClick={() => handleReviewRuntimePermissions(true)} disabled={isWorking}>
              Review Before Run
            </button>
          )}
          {isInstalledExecutable && !skill.enabled && (
            <button type="button" disabled>
              Run Disabled
            </button>
          )}
          <button type="button" onClick={handleToggleEnabled} disabled={isWorking}>
            {skill.enabled ? "Disable" : "Enable"}
          </button>
          <button type="button" className="secondary" onClick={handleRepair} disabled={isWorking}>
            Ask Agents to Repair
          </button>
          <button type="button" className="danger" onClick={handleDelete} disabled={isWorking}>
            Delete
          </button>
        </div>
      )}

      {error && <p className="error-text">{error}</p>}

      {runtimePermission && showRuntimeModal && (
        <PermissionRequestModal
          request={runtimePermission}
          title={`${skill.name} runtime permissions`}
          subject="Approve these manifest permissions before installing or running this skill."
          isWorking={isWorking}
          approveLabel="Approve Runtime Permissions"
          denyLabel="Deny"
          onApprove={handleApproveRuntimePermissions}
          onDeny={handleDenyRuntimePermissions}
        />
      )}

      {validation && <ValidationResult validation={validation} />}

      <section className="detail-panel">
        <header className="page-header">
          <div>
            <h2>Agent Runs</h2>
            <p className="muted">Build and repair workflows are tracked as bounded, observable agent runs.</p>
          </div>
          <Link to="/agent-runs">All agent runs</Link>
        </header>
        {agentRuns.length > 0 ? (
          <div className="run-list">
            {agentRuns.map((agentRun) => (
              <article key={agentRun.id} className="run-row">
                <div>
                  <strong>
                    <Link to={`/agent-runs/${agentRun.id}`}>Agent Run #{agentRun.id}</Link>
                  </strong>
                  <span>{agentRun.run_type} / {agentRun.current_step ?? "none"}</span>
                  {agentRun.summary && <span>{agentRun.summary}</span>}
                </div>
                <span className={`badge status-${agentRun.status}`}>{agentRun.status}</span>
              </article>
            ))}
          </div>
        ) : (
          <p className="muted">No agent runs are linked to this skill yet.</p>
        )}
      </section>

      <section className="detail-panel">
        <header className="page-header">
          <div>
            <h2>Schedules</h2>
            <p className="muted">Schedules require approval and still use the same runtime permission checks as manual runs.</p>
          </div>
          <Link to="/schedules">All schedules</Link>
        </header>
        <ScheduleList
          schedules={schedules}
          isWorking={isWorking}
          onApprove={(id) => handleScheduleAction(() => api.approveSchedule(id))}
          onDeny={(id) => handleScheduleAction(() => api.denySchedule(id))}
          onPause={(id) => handleScheduleAction(() => api.pauseSchedule(id))}
          onResume={(id) => handleScheduleAction(() => api.resumeSchedule(id))}
          onDelete={(id) => handleScheduleAction(() => api.deleteSchedule(id))}
          onRunNow={(id) => handleScheduleAction(() => api.runScheduleNow(id))}
        />
        {isInstalledExecutable ? (
          <form className="form-panel" onSubmit={handleCreateSchedule}>
            <h3>Create Schedule Request</h3>
            <div className="form-grid">
              <label>
                Name
                <input value={scheduleName} onChange={(event) => setScheduleName(event.target.value)} required />
              </label>
              <label>
                Type
                <select value={scheduleType} onChange={(event) => setScheduleType(event.target.value as ScheduleType)}>
                  <option value="daily">daily</option>
                  <option value="weekly">weekly</option>
                  <option value="interval">interval</option>
                </select>
              </label>
              {(scheduleType === "daily" || scheduleType === "weekly") && (
                <label>
                  Time
                  <input type="time" value={scheduleTime} onChange={(event) => setScheduleTime(event.target.value)} />
                </label>
              )}
              {scheduleType === "weekly" && (
                <label>
                  Day
                  <select value={scheduleDay} onChange={(event) => setScheduleDay(event.target.value)}>
                    {["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"].map((day) => (
                      <option key={day} value={day}>{day}</option>
                    ))}
                  </select>
                </label>
              )}
              {scheduleType === "interval" && (
                <>
                  <label>
                    Every
                    <input type="number" min="1" value={scheduleEvery} onChange={(event) => setScheduleEvery(Number(event.target.value))} />
                  </label>
                  <label>
                    Unit
                    <select value={scheduleUnit} onChange={(event) => setScheduleUnit(event.target.value as "minutes" | "hours" | "days")}>
                      <option value="minutes">minutes</option>
                      <option value="hours">hours</option>
                      <option value="days">days</option>
                    </select>
                  </label>
                </>
              )}
              <label>
                Timezone
                <input value={scheduleTimezone} onChange={(event) => setScheduleTimezone(event.target.value)} />
              </label>
            </div>
            <label>
              Input JSON
              <textarea value={scheduleInput} onChange={(event) => setScheduleInput(event.target.value)} rows={6} />
            </label>
            <div className="button-row">
              <button type="submit" disabled={isWorking}>Create Schedule Request</button>
              <button type="button" className="secondary" onClick={handleCreateManifestSchedule} disabled={isWorking}>
                Use Manifest Schedule
              </button>
            </div>
          </form>
        ) : (
          <p className="muted">Only installed automation or hybrid skills can request schedules.</p>
        )}
      </section>

      <section className="detail-panel">
        <h2>Skill Files</h2>
        {files.length > 0 ? (
          <div className="file-list">
            {files.map((file) => (
              <article key={file.path}>
                <h3>{file.path}</h3>
                <pre>{file.content}</pre>
              </article>
            ))}
          </div>
        ) : (
          <p className="muted">No readable skill files found.</p>
        )}
      </section>

      {isInstalledExecutable && (
        <section className="detail-panel">
          <header className="page-header">
            <div>
              <h2>Run Input</h2>
              <p className="muted">Use valid JSON with double-quoted property names.</p>
            </div>
            <button
              type="button"
              className="secondary"
              onClick={() => setRunInput('{\n  "hello": "world"\n}')}
            >
              Reset Example
            </button>
          </header>
          <textarea
            value={runInput}
            onChange={(event) => setRunInput(event.target.value)}
            rows={8}
            aria-label="Run input JSON"
          />
        </section>
      )}

      {isInstalledExecutable && (
        <>
          <section className="detail-panel">
            <h2>Latest Run</h2>
            {latestRun ? (
              <RunDetail run={latestRun} />
            ) : (
              <p className="muted">No runs recorded yet.</p>
            )}
          </section>

          <section className="detail-panel">
            <h2>Run History</h2>
            {runs.length > 0 ? (
              <div className="run-list">
                {runs.map((run) => (
                  <article key={run.id} className="run-row">
                    <div>
                      <strong>Run #{run.id}</strong>
                      <span>
                        {formatTimestamp(run.started_at, "not started")}
                      </span>
                    </div>
                    <span className={`badge run-${run.status}`}>{run.status}</span>
                  </article>
                ))}
              </div>
            ) : (
              <p className="muted">No run history yet.</p>
            )}
          </section>
        </>
      )}
    </section>
  );
}

function parseRunInput(value: string): Record<string, unknown> {
  const trimmed = value.trim();
  if (!trimmed) {
    return {};
  }
  try {
    return JSON.parse(trimmed) as Record<string, unknown>;
  } catch (err) {
    if (err instanceof SyntaxError) {
      throw new Error(
        'Run input must be valid JSON. Use double quotes around property names, like { "hello": "world" }.'
      );
    }
    throw err;
  }
}

function ScheduleList({
  schedules,
  isWorking,
  onApprove,
  onDeny,
  onPause,
  onResume,
  onDelete,
  onRunNow,
}: {
  schedules: SkillSchedule[];
  isWorking: boolean;
  onApprove: (id: number) => void;
  onDeny: (id: number) => void;
  onPause: (id: number) => void;
  onResume: (id: number) => void;
  onDelete: (id: number) => void;
  onRunNow: (id: number) => void;
}) {
  if (schedules.length === 0) {
    return <p className="muted">No schedules for this skill yet.</p>;
  }
  return (
    <div className="run-list">
      {schedules.map((schedule) => (
        <article key={schedule.id} className="run-row">
          <div>
            <strong>{schedule.name}</strong>
            <span>{humanSchedule(schedule)}</span>
            <span>
              Last: {formatTimestamp(schedule.last_run_at, "never")}
              {schedule.last_run_status ? ` (${schedule.last_run_status})` : ""}
            </span>
          </div>
          <div className="button-row">
            <span className={`badge status-${schedule.status}`}>{schedule.status}</span>
            {schedule.status === "pending" && (
              <>
                <button type="button" onClick={() => onApprove(schedule.id)} disabled={isWorking}>Approve</button>
                <button type="button" className="secondary" onClick={() => onDeny(schedule.id)} disabled={isWorking}>Deny</button>
              </>
            )}
            {schedule.status === "active" && (
              <button type="button" className="secondary" onClick={() => onPause(schedule.id)} disabled={isWorking}>Pause</button>
            )}
            {schedule.status === "paused" && (
              <button type="button" onClick={() => onResume(schedule.id)} disabled={isWorking}>Resume</button>
            )}
            <button type="button" className="secondary" onClick={() => onRunNow(schedule.id)} disabled={isWorking || schedule.status === "paused"}>
              Run Now
            </button>
            <button type="button" className="danger" onClick={() => onDelete(schedule.id)} disabled={isWorking}>Delete</button>
          </div>
        </article>
      ))}
    </div>
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

function ValidationResult({ validation }: { validation: ProposedSkillValidation }) {
  return (
    <section className="detail-panel">
      <h2>Validation Result</h2>
      <dl className="detail-grid">
        <div>
          <dt>Status</dt>
          <dd>{validation.ok ? "passed" : "failed"}</dd>
        </div>
        <div>
          <dt>Manifest</dt>
          <dd>{validation.manifest_valid ? "valid" : "invalid"}</dd>
        </div>
        <div>
          <dt>Tests</dt>
          <dd>
            {validation.tests_run
              ? validation.tests_passed
                ? "passed"
                : "failed"
              : "not run"}
          </dd>
        </div>
      </dl>
      {validation.error_message && (
        <div className="run-detail">
          <h3>Error</h3>
          <pre>{validation.error_message}</pre>
        </div>
      )}
      {validation.warnings.length > 0 && (
        <div className="run-detail">
          <h3>Warnings</h3>
          <pre>{validation.warnings.join("\n")}</pre>
        </div>
      )}
      {(validation.stdout || validation.stderr) && (
        <div className="run-detail">
          <h3>Test Output</h3>
          <pre>{validation.stdout || "No stdout captured."}</pre>
          <h3>Test Errors</h3>
          <pre>{validation.stderr || "No stderr captured."}</pre>
        </div>
      )}
    </section>
  );
}

function RunDetail({ run }: { run: SkillRun }) {
  return (
    <div className="run-detail">
      <dl className="detail-grid">
        <div>
          <dt>Status</dt>
          <dd>{run.status}</dd>
        </div>
        <div>
          <dt>Exit Code</dt>
          <dd>{run.exit_code ?? "none"}</dd>
        </div>
        <div>
          <dt>Started</dt>
          <dd>{formatTimestamp(run.started_at, "not started")}</dd>
        </div>
        <div>
          <dt>Ended</dt>
          <dd>{formatTimestamp(run.ended_at, "not ended")}</dd>
        </div>
      </dl>

      {run.error_message && (
        <div>
          <h3>Error</h3>
          <pre>{run.error_message}</pre>
        </div>
      )}
      {run.output_json && (
        <div>
          <h3>Output JSON</h3>
          <pre>{JSON.stringify(run.output_json, null, 2)}</pre>
        </div>
      )}
      <div>
        <h3>Stdout</h3>
        <pre>{run.stdout || "No stdout captured."}</pre>
      </div>
      <div>
        <h3>Stderr</h3>
        <pre>{run.stderr || "No stderr captured."}</pre>
      </div>
    </div>
  );
}
