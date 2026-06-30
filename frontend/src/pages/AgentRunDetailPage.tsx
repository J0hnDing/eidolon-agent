import { Link, useNavigate, useParams } from "react-router-dom";
import { useEffect, useState } from "react";

import { AgentRunDetail, api } from "../api/client";

export default function AgentRunDetailPage() {
  const { agentRunId } = useParams();
  const navigate = useNavigate();
  const [run, setRun] = useState<AgentRunDetail | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isWorking, setIsWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadRun();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentRunId]);

  async function loadRun() {
    const id = Number(agentRunId);
    if (!Number.isInteger(id)) {
      setError("Invalid agent run id.");
      setIsLoading(false);
      return;
    }
    setIsLoading(true);
    setError(null);
    try {
      setRun(await api.getAgentRun(id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load agent run");
    } finally {
      setIsLoading(false);
    }
  }

  async function handleCancel() {
    if (!run) return;
    setIsWorking(true);
    setError(null);
    try {
      await api.cancelAgentRun(run.id);
      await loadRun();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not cancel agent run");
    } finally {
      setIsWorking(false);
    }
  }

  async function handleRetry(stepId: number) {
    if (!run) return;
    setIsWorking(true);
    setError(null);
    try {
      await api.retryAgentRunStep(run.id, stepId);
      await loadRun();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not retry step");
    } finally {
      setIsWorking(false);
    }
  }

  async function handleResume() {
    if (!run) return;
    setIsWorking(true);
    setError(null);
    try {
      await api.resumeAgentRun(run.id);
      await loadRun();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not resume agent run");
    } finally {
      setIsWorking(false);
    }
  }

  async function handleRetryCurrentMilestone() {
    if (!run) return;
    setIsWorking(true);
    setError(null);
    try {
      await api.retryCurrentMilestone(run.id);
      await loadRun();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not retry current milestone");
    } finally {
      setIsWorking(false);
    }
  }

  async function handleDelete() {
    if (!run) return;
    setIsWorking(true);
    setError(null);
    try {
      await api.deleteAgentRun(run.id);
      navigate("/agent-runs");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not delete agent run");
      setIsWorking(false);
    }
  }

  if (isLoading) return <p className="muted">Loading agent run...</p>;

  if (error || !run) {
    return (
      <section className="page stack">
        <Link to="/agent-runs">Back to agent runs</Link>
        <p className="error-text">{error ?? "Agent run not found"}</p>
      </section>
    );
  }

  return (
    <section className="page stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">Agent Run</p>
          <h1>Run #{run.id}</h1>
        </div>
        <Link to="/agent-runs">Back to agent runs</Link>
      </header>

      <section className="detail-panel">
        <p>{run.user_request}</p>
        <dl className="detail-grid">
          <div>
            <dt>Status</dt>
            <dd>{run.status}</dd>
          </div>
          <div>
            <dt>Type</dt>
            <dd>{run.run_type}</dd>
          </div>
          <div>
            <dt>Current Step</dt>
            <dd>{run.current_step ?? "none"}</dd>
          </div>
          <div>
            <dt>Current Milestone</dt>
            <dd>{run.current_milestone ?? "none"}</dd>
          </div>
          <div>
            <dt>Skill</dt>
            <dd>{run.skill_id ? <Link to={`/skills/${run.skill_id}`}>Skill #{run.skill_id}</Link> : "none"}</dd>
          </div>
          <div>
            <dt>Generation Request</dt>
            <dd>{run.generation_request_id ?? "none"}</dd>
          </div>
          <div>
            <dt>Created</dt>
            <dd>{formatTimestamp(run.created_at, "unknown")}</dd>
          </div>
          <div>
            <dt>Completed</dt>
            <dd>{formatTimestamp(run.completed_at, "not completed")}</dd>
          </div>
        </dl>
        {run.summary && <p className="muted">{run.summary}</p>}
        {run.error_message && <p className="error-text">{run.error_message}</p>}
        <div className="button-row">
          <button
            type="button"
            onClick={handleResume}
            disabled={isWorking || !["waiting_for_approval", "pending"].includes(run.status)}
          >
            Resume
          </button>
          <button
            type="button"
            className="secondary"
            onClick={handleRetryCurrentMilestone}
            disabled={isWorking || !["failed", "blocked"].includes(run.status)}
          >
            Retry Current Milestone
          </button>
          <button
            type="button"
            className="secondary"
            onClick={handleCancel}
            disabled={isWorking || ["succeeded", "failed", "cancelled"].includes(run.status)}
          >
            Cancel
          </button>
          <button type="button" className="danger" onClick={handleDelete} disabled={isWorking}>
            Delete Run
          </button>
        </div>
      </section>

      <section className="detail-panel">
        <h2>Blueprint</h2>
        {run.blueprint_json ? <pre>{JSON.stringify(run.blueprint_json, null, 2)}</pre> : <p className="muted">No blueprint recorded.</p>}
      </section>

      <section className="detail-panel">
        <h2>Failure Counts</h2>
        <pre>{JSON.stringify(run.failure_count_json ?? {}, null, 2)}</pre>
      </section>

      {run.final_summary_json && (
        <section className="detail-panel">
          <h2>Final Summary</h2>
          <pre>{JSON.stringify(run.final_summary_json, null, 2)}</pre>
        </section>
      )}

      <section className="detail-panel">
        <h2>Steps</h2>
        <div className="agent-step-list">
          {run.steps.map((step) => (
            <article className="agent-step" key={step.id}>
              <header className="agent-step-header">
                <div>
                  <h3>{step.step_name}</h3>
                  <p className="muted">
                    {step.milestone_name ? `${step.milestone_name} / ` : ""}
                    {formatTimestamp(step.started_at, "not started")} - {formatTimestamp(step.ended_at, "not ended")}
                  </p>
                </div>
                <span className={`badge status-${step.status}`}>{step.status}</span>
              </header>
              {step.logs && <p>{step.logs}</p>}
              {step.error_message && <p className="error-text">{step.error_message}</p>}
              <details>
                <summary>Input JSON</summary>
                <pre>{JSON.stringify(step.input_json ?? {}, null, 2)}</pre>
              </details>
              <details>
                <summary>Output JSON</summary>
                <pre>{JSON.stringify(step.output_json ?? {}, null, 2)}</pre>
              </details>
              {(step.status === "failed" || step.status === "blocked") && (
                <div className="button-row">
                  <button type="button" onClick={() => handleRetry(step.id)} disabled={isWorking}>
                    Retry Current Milestone
                  </button>
                </div>
              )}
            </article>
          ))}
        </div>
      </section>
    </section>
  );
}

function formatTimestamp(value: string | null, fallback: string): string {
  if (!value) return fallback;
  const hasTimezone = /(?:z|[+-]\d{2}:?\d{2})$/i.test(value);
  return new Date(hasTimezone ? value : `${value}Z`).toLocaleString();
}
