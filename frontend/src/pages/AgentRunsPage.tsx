import { Link } from "react-router-dom";
import { useEffect, useState } from "react";

import { AgentRun, api } from "../api/client";
import { formatSystemDateTime } from "../lib/dateTime";
import "./records.css";
import { AppIcon } from "../components/AppIcon";
import { usePolling } from "../lib/usePolling";

const LIVE_RUN_STATUSES = new Set(["pending", "running", "waiting_for_approval"]);

interface AgentRunsPageProps {
  embedded?: boolean;
}

export default function AgentRunsPage({ embedded = false }: AgentRunsPageProps) {
  const [runs, setRuns] = useState<AgentRun[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");

  useEffect(() => {
    loadRuns();
  }, []);

  usePolling(
    () => loadRuns({ showLoading: false }),
    runs.some((run) => LIVE_RUN_STATUSES.has(run.status)),
    2000,
  );

  async function loadRuns(options: { showLoading?: boolean } = {}) {
    if (options.showLoading !== false) setIsLoading(true);
    setError(null);
    try {
      setRuns(await api.listAgentRuns());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load agent runs");
    } finally {
      if (options.showLoading !== false) setIsLoading(false);
    }
  }

  const visibleRuns = runs.filter((run) => {
    const matchesStatus = filter === "all" || (filter === "live" ? LIVE_RUN_STATUSES.has(run.status) : filter === "attention" ? ["paused", "failed", "blocked"].includes(run.status) : ["succeeded", "cancelled"].includes(run.status));
    return matchesStatus && `${run.id} ${run.user_request} ${run.run_type} ${run.status}`.toLowerCase().includes(query.trim().toLowerCase());
  });
  return (
    <section className={embedded ? "stack activity-embedded-content execution-workspace" : "page stack records-page execution-workspace"}>
      {!embedded && <header className="page-header"><div><h1>Agent Runs</h1><p className="muted">Follow build, repair, and test workflows from request to result.</p></div></header>}
      <div className="approval-overview-line"><div><h2>Execution history</h2><p className="muted">Every build and repair, with a clear trail from request to result.</p></div><button type="button" className="secondary" disabled={isLoading} onClick={() => void loadRuns()}>Refresh</button></div>
      <div className="records-toolbar"><label className="records-search"><AppIcon name="search" size={16} /><input aria-label="Search executions" placeholder="Search executions…" value={query} onChange={(event) => setQuery(event.target.value)} /></label><label className="execution-status-filter"><span className="muted">Status</span><select aria-label="Filter executions by status" value={filter} onChange={(event) => setFilter(event.target.value)}><option value="all">All executions</option><option value="live">In progress</option><option value="attention">Needs attention</option><option value="completed">Completed</option></select></label><span className="muted">{visibleRuns.length} {visibleRuns.length === 1 ? "execution" : "executions"}</span></div>
      {error && <p className="error-text" role="alert">{error}</p>}
      {isLoading && <p className="muted" role="status">Loading agent runs...</p>}
      <div className="execution-list">
        {visibleRuns.length > 0 && <div className="execution-list-heading" aria-hidden="true"><span>Request</span><span>Status</span><span>Workflow</span><span>Created</span><span /></div>}
        {visibleRuns.map((run) => <Link className="execution-list-row" key={run.id} to={embedded ? `/activity/runs/${run.id}` : `/agent-runs/${run.id}`}><div className="execution-request"><span className="execution-run-id">Run #{run.id}</span><strong>{run.user_request}</strong>{(run.current_step || run.summary) && <small>{run.current_step ? formatStepName(run.current_step) : run.summary}{run.current_task_id ? ` · ${run.current_task_id}` : ""}</small>}</div><span className={`record-request-status status-${run.status}`}><span className="record-state-dot" aria-hidden="true" />{run.status.replace(/_/g, " ")}</span><span className="execution-type">{run.run_type.replace(/_/g, " ")}{run.skill_id && <small>Skill #{run.skill_id}</small>}</span><time dateTime={run.created_at}>{formatTimestamp(run.created_at)}</time><span className="execution-row-arrow" aria-hidden="true">→</span></Link>)}
        {!isLoading && visibleRuns.length === 0 && <div className="record-quiet-empty execution-empty"><AppIcon name="clock" size={30} /><h3>{runs.length ? "No matching executions" : "A clear trail of your work"}</h3><p>{runs.length ? "Try another search or status filter." : "Skill builds and repairs appear here, from the first step to the final result."}</p>{!runs.length && <Link className="button-link secondary" to="/skills/build">Build a skill</Link>}</div>}
      </div>
    </section>
  );
}

function formatStepName(value: string): string {
  if (value === "product_manager") return "Product Manager";
  if (value === "backend") return "Backend";
  if (value === "builder") return "Builder";
  if (value === "tester") return "Tester";
  return value;
}

function formatTimestamp(value: string): string {
  return formatSystemDateTime(value);
}
