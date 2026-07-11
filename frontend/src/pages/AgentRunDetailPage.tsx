import { Link, useNavigate, useParams } from "react-router-dom";
import { useEffect, useState } from "react";

import { AgentRunDetail, api } from "../api/client";
import { usePolling } from "../lib/usePolling";

const LIVE_RUN_STATUSES = new Set(["pending", "running", "waiting_for_approval"]);

interface TaskDagNode {
  id: string;
  title?: string;
  summary?: string;
  depends_on?: string[];
  expected_output_paths?: string[];
  file_write_claims?: string[];
  backend_api_ids?: number[];
}

interface TaskDagEdge {
  from?: string;
  to?: string;
  reason?: string;
}

interface TaskDag {
  graph_id?: string;
  root_task_ids?: string[];
  nodes?: TaskDagNode[];
  edges?: TaskDagEdge[];
}

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

  usePolling(
    () => loadRun({ showLoading: false }),
    Boolean(run && LIVE_RUN_STATUSES.has(run.status)),
    1500,
  );

  async function loadRun(options: { showLoading?: boolean } = {}) {
    const id = Number(agentRunId);
    if (!Number.isInteger(id)) {
      setError("Invalid agent run id.");
      setIsLoading(false);
      return;
    }
    if (options.showLoading !== false) setIsLoading(true);
    setError(null);
    try {
      setRun(await api.getAgentRun(id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load agent run");
    } finally {
      if (options.showLoading !== false) setIsLoading(false);
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
      await api.retryCurrentTask(run.id);
      await loadRun();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not retry current task");
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

  const taskDag = extractTaskDag(run);
  const taskStatuses = extractTaskStatuses(run);

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
            <dt>Current Task Node</dt>
            <dd>{run.current_task_id ?? run.current_milestone ?? "none"}</dd>
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
          <div>
            <dt>Build Tokens</dt>
            <dd>{formatTokens(run.total_tokens)}</dd>
          </div>
        </dl>
        {run.summary && <p className="muted">{run.summary}</p>}
        {run.pause_reason && <p className="error-text">{run.pause_reason}</p>}
        {run.error_message && <p className="error-text">{run.error_message}</p>}
        <div className="button-row">
          <button
            type="button"
            onClick={handleResume}
            disabled={isWorking || !["waiting_for_approval", "pending", "paused"].includes(run.status)}
          >
            Resume
          </button>
          <button
            type="button"
            className="secondary"
            onClick={handleRetryCurrentMilestone}
            disabled={isWorking || !["failed", "blocked"].includes(run.status)}
          >
            Retry Current Task
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

      <section className="detail-panel">
        <h2>Task DAG</h2>
        {taskDag ? (
          <TaskDagView dag={taskDag} statuses={taskStatuses} currentTaskId={run.current_task_id ?? run.current_milestone} steps={run.steps} />
        ) : (
          <p className="muted">No task DAG recorded yet.</p>
        )}
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
                    {(step.task_node_id ?? step.milestone_name) ? `${step.task_node_id ?? step.milestone_name} / ` : ""}
                    {formatTimestamp(step.started_at, "not started")} - {formatTimestamp(step.ended_at, "not ended")}
                  </p>
                </div>
                <span className={`badge status-${step.status}`}>{step.status}</span>
              </header>
              {step.logs && <p>{step.logs}</p>}
              {step.total_tokens > 0 && (
                <p className="muted">
                  {formatTokens(step.total_tokens)} tokens ({formatTokens(step.input_tokens)} input, {formatTokens(step.output_tokens)} output)
                </p>
              )}
              {step.codex_invocations_json.length > 0 && (
                <details>
                  <summary>Codex invocations</summary>
                  <pre>{JSON.stringify(step.codex_invocations_json, null, 2)}</pre>
                </details>
              )}
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
                    Retry Current Task
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

function TaskDagView({
  dag,
  statuses,
  currentTaskId,
  steps,
}: {
  dag: TaskDag;
  statuses: Record<string, string>;
  currentTaskId: string | null;
  steps: AgentRunDetail["steps"];
}) {
  const nodes = Array.isArray(dag.nodes) ? dag.nodes : [];
  const explicitEdges = Array.isArray(dag.edges) ? dag.edges : [];
  const dependencyEdges = nodes.flatMap((node) =>
    Array.isArray(node.depends_on)
      ? node.depends_on.map((dependency) => ({ from: dependency, to: node.id, reason: "dependency" }))
      : [],
  );
  const edges = explicitEdges.length > 0 ? explicitEdges : dependencyEdges;

  return (
    <div className="task-dag">
      <div className="task-dag-meta">
        <span>Graph: {dag.graph_id ?? "unnamed"}</span>
        <span>Roots: {Array.isArray(dag.root_task_ids) && dag.root_task_ids.length ? dag.root_task_ids.join(", ") : "none"}</span>
      </div>
      {edges.length > 0 && (
        <div className="task-dag-edges">
          {edges.map((edge, index) => (
            <span key={`${edge.from ?? "unknown"}-${edge.to ?? "unknown"}-${index}`}>
              {edge.from ?? "unknown"} {"->"} {edge.to ?? "unknown"}
              {edge.reason ? ` (${edge.reason})` : ""}
            </span>
          ))}
        </div>
      )}
      <div className="task-dag-nodes">
        {nodes.map((node) => {
          const status = statuses[node.id] ?? (node.id === currentTaskId ? "active" : "pending");
          const tokens = steps
            .filter((step) => (step.task_node_id ?? step.milestone_name) === node.id)
            .reduce((total, step) => total + step.total_tokens, 0);
          return (
            <article className="task-dag-node" key={node.id}>
              <header>
                <div>
                  <h3>{node.title || node.id}</h3>
                  <p className="muted">{node.id}</p>
                </div>
                <span className={`badge status-${status}`}>{status}</span>
              </header>
              {node.summary && <p>{node.summary}</p>}
              <dl className="compact-grid detail-grid">
                <div>
                  <dt>Depends On</dt>
                  <dd>{formatList(node.depends_on)}</dd>
                </div>
                <div>
                  <dt>Output Paths</dt>
                  <dd>{formatList(node.expected_output_paths)}</dd>
                </div>
                <div>
                  <dt>Write Claims</dt>
                  <dd>{formatList(node.file_write_claims)}</dd>
                </div>
                <div>
                  <dt>Backend APIs</dt>
                  <dd>{formatList((node.backend_api_ids ?? []).map(String))}</dd>
                </div>
                <div>
                  <dt>Codex Tokens</dt>
                  <dd>{formatTokens(tokens)}</dd>
                </div>
              </dl>
            </article>
          );
        })}
      </div>
    </div>
  );
}

function extractTaskDag(run: AgentRunDetail): TaskDag | null {
  for (const step of run.steps) {
    const output = step.output_json;
    const taskDag = output?.task_dag_json;
    if (isTaskDag(taskDag)) return taskDag;
  }
  return null;
}

function extractTaskStatuses(run: AgentRunDetail): Record<string, string> {
  const taskStatuses = run.final_summary_json?.task_statuses;
  if (!taskStatuses || typeof taskStatuses !== "object" || Array.isArray(taskStatuses)) return {};
  return Object.fromEntries(
    Object.entries(taskStatuses).filter((entry): entry is [string, string] => typeof entry[1] === "string"),
  );
}

function isTaskDag(value: unknown): value is TaskDag {
  return Boolean(value && typeof value === "object" && Array.isArray((value as TaskDag).nodes));
}

function formatList(value: string[] | undefined): string {
  return value && value.length ? value.join(", ") : "none";
}

function formatTimestamp(value: string | null, fallback: string): string {
  if (!value) return fallback;
  const hasTimezone = /(?:z|[+-]\d{2}:?\d{2})$/i.test(value);
  return new Date(hasTimezone ? value : `${value}Z`).toLocaleString();
}

function formatTokens(value: number): string {
  return new Intl.NumberFormat().format(value);
}
