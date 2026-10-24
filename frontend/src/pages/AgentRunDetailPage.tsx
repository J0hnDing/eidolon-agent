import { Link, useNavigate, useParams } from "react-router-dom";
import { useEffect, useState } from "react";

import { AgentRunDetail, SkillRun, api } from "../api/client";
import { DeleteIconButton } from "../components/DeleteIconButton";
import { Modal } from "../components/Modal";
import "./records.css";
import "./execution-details.css";
import { RunHistory } from "../features/skill-detail/SkillDetailPanels";
import "../features/skill-detail/skill-detail.css";
import { canRetryAgentRun } from "../features/agent-run/agentRunActions";
import { formatSystemDateTime } from "../lib/dateTime";
import { usePolling } from "../lib/usePolling";

const LIVE_RUN_STATUSES = new Set(["pending", "running", "waiting_for_approval"]);

interface TaskDagNode {
  id: string;
  task_prompt?: string;
  depends_on?: string[];
  write_paths?: string[];
  function_ids?: string[];
}

interface TaskDag {
  nodes?: TaskDagNode[];
}

interface AgentRunDetailPageProps {
  embedded?: boolean;
  routeAgentRunId?: string;
}

export default function AgentRunDetailPage({ embedded = false, routeAgentRunId }: AgentRunDetailPageProps) {
  const { agentRunId: paramAgentRunId } = useParams();
  const agentRunId = routeAgentRunId ?? paramAgentRunId;
  const navigate = useNavigate();
  const runsPath = embedded ? "/activity/runs" : "/agent-runs";
  const [run, setRun] = useState<AgentRunDetail | null>(null);
  const [skillRuns, setSkillRuns] = useState<SkillRun[]>([]);
  const [activeTab, setActiveTab] = useState<"build" | "artifacts" | "run-history">("build");
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isWorking, setIsWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [selectedStepId, setSelectedStepId] = useState<number | null>(null);
  const [stepQuery, setStepQuery] = useState("");
  const [stepStatus, setStepStatus] = useState("");
  const [artifact, setArtifact] = useState("blueprint");

  useEffect(() => {
    loadRun();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentRunId]);

  useEffect(() => {
    loadSkillRuns();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.skill_id]);

  usePolling(
    () => loadRun({ showLoading: false }),
    Boolean(run && LIVE_RUN_STATUSES.has(run.status)),
    1500,
  );

  usePolling(
    loadSkillRuns,
    Boolean(run?.skill_id && activeTab === "run-history"),
    5000,
  );

  async function loadSkillRuns() {
    if (!run?.skill_id) {
      setSkillRuns([]);
      setHistoryError(null);
      return;
    }
    try {
      setSkillRuns(await api.listSkillRuns(run.skill_id));
      setHistoryError(null);
    } catch (err) {
      setHistoryError(err instanceof Error ? err.message : "Could not load skill run history");
    }
  }

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

  async function handleRetryCurrentTask() {
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
      navigate(runsPath);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not delete agent run");
      setIsWorking(false);
    }
  }

  if (isLoading) return <p className="muted" role="status">Loading agent run...</p>;

  if (!run) {
    return (
      <section className={embedded ? "stack activity-embedded-content execution-detail-workspace" : "page stack records-page execution-detail-workspace"}>
        <Link to={runsPath}>{embedded ? "Back to executions" : "Back to agent runs"}</Link>
        <p className="error-text" role="alert">{error ?? "Agent run not found"}</p>
      </section>
    );
  }

  const taskDag = extractTaskDag(run);
  const taskStatuses = extractTaskStatuses(run);
  const canRetry = canRetryAgentRun(run);
  const selectedStep = run.steps.find((step) => step.id === selectedStepId);
  const visibleSteps = run.steps.filter((step) => (!stepStatus || step.status === stepStatus)
    && `${formatStepName(step.step_name)} ${step.task_node_id ?? ""} ${step.action ?? ""} ${step.error_message ?? ""}`.toLowerCase().includes(stepQuery.trim().toLowerCase()));

  return (
    <section className={embedded ? "stack activity-embedded-content execution-detail-workspace" : "page stack records-page execution-detail-workspace"}>
      {!embedded && <header className="page-header">
        <div>
          <h1>Run #{run.id}</h1>
        </div>
        <Link to={runsPath}>Back to agent runs</Link>
      </header>}
      {embedded && <div className="activity-run-back"><Link to={runsPath}>Back to executions</Link></div>}

      {error && <p className="error-text" role="alert">{error}</p>}
      <section className="execution-detail-overview">
        <div className="execution-detail-main"><header className="execution-detail-title"><span className="approval-detail-breadcrumb">Execution #{run.id} · {run.run_type.replace(/_/g, " ")}</span><h2>{run.user_request}</h2><span className={`record-request-status status-${run.status}`}><span className="record-state-dot" aria-hidden="true" />{run.status.replace(/_/g, " ")}</span></header>{run.summary && <p className="execution-result-summary">{run.summary}</p>}{run.pause_reason && <p className="error-text">{run.pause_reason}</p>}{run.error_message && <p className="error-text">{run.error_message}</p>}<div className="button-row execution-action-bar"><button type="button" onClick={handleResume} disabled={isWorking || !["waiting_for_approval", "pending", "paused"].includes(run.status)}>Resume</button>{canRetry && <button type="button" className="secondary" onClick={handleRetryCurrentTask} disabled={isWorking}>Retry Current Task</button>}<button type="button" className="secondary" onClick={handleCancel} disabled={isWorking || ["succeeded", "failed", "cancelled"].includes(run.status)}>Cancel</button><DeleteIconButton label="Delete run" onClick={() => setDeleteOpen(true)} disabled={isWorking} /></div></div>
        <aside className="execution-properties"><h3>Properties</h3><dl className="record-properties"><div><dt>Current step</dt><dd>{run.current_step ? formatStepName(run.current_step) : "—"}</dd></div><div><dt>Task</dt><dd>{run.current_task_id ?? "—"}</dd></div><div><dt>Skill</dt><dd>{run.skill_id ? <Link to={`/skills/${run.skill_id}`}>Skill #{run.skill_id}</Link> : "—"}</dd></div><div><dt>Created</dt><dd>{formatTimestamp(run.created_at, "unknown")}</dd></div><div><dt>Completed</dt><dd>{formatTimestamp(run.completed_at, "Not completed")}</dd></div><div><dt>Build tokens</dt><dd>{formatTokens(run.total_tokens)}</dd></div><div><dt>Generation request</dt><dd>{run.generation_request_id ?? "—"}</dd></div></dl></aside>
      </section>

      <div className="agent-run-tabs" role="tablist" aria-label="Agent run views">
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === "build"}
          className={activeTab === "build" ? "active" : ""}
          onClick={() => setActiveTab("build")}
        >
          Workflow
        </button>
        <button type="button" role="tab" aria-selected={activeTab === "artifacts"} className={activeTab === "artifacts" ? "active" : ""} onClick={() => setActiveTab("artifacts")}>Build artifacts</button>
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === "run-history"}
          className={activeTab === "run-history" ? "active" : ""}
          onClick={() => setActiveTab("run-history")}
          disabled={!run.skill_id}
        >
          Skill Run History
        </button>
      </div>

      {activeTab === "build" && <section className="execution-timeline">
        <div className="section-heading"><h2>Workflow steps</h2><span className="muted">{visibleSteps.length} of {run.steps.length} steps</span></div>
        <div className="execution-step-filters">
          <input type="search" aria-label="Search workflow steps" placeholder="Search steps, tasks, or errors…" value={stepQuery} onChange={(event) => setStepQuery(event.target.value)} />
          <select aria-label="Filter workflow steps by status" value={stepStatus} onChange={(event) => setStepStatus(event.target.value)}><option value="">All statuses</option>{[...new Set(run.steps.map((step) => step.status))].map((status) => <option key={status} value={status}>{status.replace(/_/g, " ")}</option>)}</select>
        </div>
        <div className="execution-step-list">{visibleSteps.map((step) => <button type="button" className="execution-step-row" key={step.id} aria-label={`Inspect ${formatStepName(step.step_name)} step ${step.id}`} onClick={() => setSelectedStepId(step.id)}>
          <span><strong>{formatStepName(step.step_name)}{step.task_node_id ? ` · ${step.task_node_id}` : ""}</strong><small>{step.action || formatTimestamp(step.started_at, "Not started")}</small>{step.error_message && <small className="error-text">{step.error_message}</small>}</span>
          <span className={`badge status-${step.status}`}>{step.status.replace(/_/g, " ")}</span><span className="execution-step-tokens">{formatTokens(step.total_tokens)} tokens</span><span aria-hidden="true">→</span>
        </button>)}</div>
        {!visibleSteps.length && <div className="record-quiet-empty"><p>{run.steps.length ? "No steps match these filters." : "No steps recorded yet."}</p>{run.steps.length > 0 && <button type="button" className="secondary compact" onClick={() => { setStepQuery(""); setStepStatus(""); }}>Clear filters</button>}</div>}
      </section>}
      {activeTab === "artifacts" && <section className="execution-artifact-reader">
        <div className="section-heading"><h2>Build artifacts</h2><select aria-label="Choose build artifact" value={artifact} onChange={(event) => setArtifact(event.target.value)}><option value="blueprint">Blueprint</option><option value="graph">Task graph</option><option value="failures">Failure counts</option><option value="summary">Final summary</option></select></div>
        {artifact === "blueprint" && (run.blueprint_json ? <pre>{JSON.stringify(run.blueprint_json, null, 2)}</pre> : <p className="muted">No blueprint recorded.</p>)}
        {artifact === "graph" && (taskDag ? <TaskDagView dag={taskDag} statuses={taskStatuses} currentTaskId={run.current_task_id} steps={run.steps} /> : <p className="muted">No task DAG recorded yet.</p>)}
        {artifact === "failures" && <pre>{JSON.stringify(run.failure_count_json ?? {}, null, 2)}</pre>}
        {artifact === "summary" && (run.final_summary_json ? <pre>{JSON.stringify(run.final_summary_json, null, 2)}</pre> : <p className="muted">No final summary recorded.</p>)}
      </section>}
      {activeTab === "run-history" && <section className="execution-runtime-history"><h2>Skill Run History</h2><p className="muted">Runtime Codex tokens are tracked separately from the build totals.</p>{historyError ? <p className="error-text" role="alert">{historyError}</p> : <RunHistory runs={skillRuns} />}</section>}
      {selectedStep && <Modal title={`${formatStepName(selectedStep.step_name)} · Step ${selectedStep.id}`} className="execution-step-modal" onClose={() => { if (!isWorking) setSelectedStepId(null); }}>
        <StepReader key={selectedStep.id} step={selectedStep} />
        {error && <p className="error-text" role="alert">{error}</p>}
        {canRetry && ["failed", "blocked"].includes(selectedStep.status) && <div className="button-row"><button type="button" disabled={isWorking} onClick={() => void handleRetry(selectedStep.id)}>Retry Current Task</button></div>}
      </Modal>}
      {deleteOpen && <Modal title="Delete execution history" onClose={() => { if (!isWorking) setDeleteOpen(false); }}><p>Delete execution #{run.id} and its recorded build history?</p><div className="button-row"><button type="button" className="secondary" disabled={isWorking} onClick={() => setDeleteOpen(false)}>Keep execution</button><button type="button" className="danger" disabled={isWorking} onClick={() => void handleDelete()}>Delete execution</button></div></Modal>}
    </section>
  );
}

function StepReader({ step }: { step: AgentRunDetail["steps"][number] }) {
  const [view, setView] = useState("log");
  const views = step.step_name === "backend"
    ? [["log", "Log"], ["calls", "Codex invocations"]]
    : [["log", "Log"], ["input", "Agent input"], ["output", "Agent output"], ["calls", "Codex invocations"]];
  return <div className="execution-step-reader">
    <dl className="record-properties"><div><dt>Status</dt><dd><span className={`badge status-${step.status}`}>{step.status}</span></dd></div><div><dt>Task</dt><dd>{step.task_node_id ?? "—"}</dd></div><div><dt>Started</dt><dd>{formatTimestamp(step.started_at, "Not started")}</dd></div><div><dt>Ended</dt><dd>{formatTimestamp(step.ended_at, "Not ended")}</dd></div><div><dt>Tokens</dt><dd>{formatTokens(step.total_tokens)} ({formatTokens(step.input_tokens)} input, {formatTokens(step.output_tokens)} output)</dd></div></dl>
    {step.action && <p>{step.action}</p>}{step.error_message && <p className="error-text">{step.error_message}</p>}
    <div className="page-tabs" role="group" aria-label="Step details">{views.map(([id, label]) => <button key={id} type="button" aria-pressed={view === id} className={view === id ? "active" : ""} onClick={() => setView(id)}>{label}</button>)}</div>
    {view === "log" && (step.logs ? <pre>{step.logs}</pre> : <p className="muted">No log recorded.</p>)}
    {view === "input" && (step.agent_input_text != null ? <pre>{step.agent_input_text}</pre> : <p className="muted">Exact agent input is unavailable for this historical step.</p>)}
    {view === "output" && (step.agent_output_text != null ? <pre>{step.agent_output_text}</pre> : <p className="muted">Exact agent output is unavailable for this historical step.</p>)}
    {view === "calls" && (step.codex_invocations_json.length ? <pre>{JSON.stringify(step.codex_invocations_json, null, 2)}</pre> : <p className="muted">No Codex invocations recorded.</p>)}
  </div>;
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
  const dependencyEdges = nodes.flatMap((node) =>
    Array.isArray(node.depends_on)
      ? node.depends_on.map((dependency) => ({ from: dependency, to: node.id }))
      : [],
  );
  const edges = dependencyEdges;

  return (
    <div className="task-dag">
      {edges.length > 0 && (
        <div className="task-dag-edges">
          {edges.map((edge, index) => (
            <span key={`${edge.from ?? "unknown"}-${edge.to ?? "unknown"}-${index}`}>
              {edge.from ?? "unknown"} {"->"} {edge.to ?? "unknown"}
            </span>
          ))}
        </div>
      )}
      <div className="task-dag-nodes">
        {nodes.map((node) => {
          const status = statuses[node.id] ?? (node.id === currentTaskId ? "active" : "pending");
          const tokens = steps
            .filter((step) => step.task_node_id === node.id)
            .reduce((total, step) => total + step.total_tokens, 0);
          return (
            <article className="task-dag-node" key={node.id}>
              <header>
                <h3>{node.id}</h3>
                <span className={`badge status-${status}`}>{status}</span>
              </header>
              {node.task_prompt && <p>{node.task_prompt}</p>}
              <dl className="compact-grid detail-grid">
                <div>
                  <dt>Depends On</dt>
                  <dd>{formatList(node.depends_on)}</dd>
                </div>
                <div>
                  <dt>Write Paths</dt>
                  <dd>{formatList(node.write_paths)}</dd>
                </div>
                <div>
                  <dt>Functions</dt>
                  <dd>{formatList(node.function_ids)}</dd>
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
  return formatSystemDateTime(value, fallback);
}

function formatStepName(value: string): string {
  if (value === "product_manager") return "Product Manager";
  if (value === "backend") return "Backend";
  if (value === "builder") return "Builder";
  if (value === "tester") return "Tester";
  return value;
}

function formatTokens(value: number): string {
  return new Intl.NumberFormat().format(value);
}
