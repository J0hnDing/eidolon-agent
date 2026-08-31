import { Link } from "react-router-dom";
import { useEffect, useState } from "react";

import { AgentRun, api } from "../api/client";
import { formatSystemDateTime } from "../lib/dateTime";
import { usePolling } from "../lib/usePolling";

const LIVE_RUN_STATUSES = new Set(["pending", "running", "waiting_for_approval"]);

export default function AgentRunsPage() {
  const [runs, setRuns] = useState<AgentRun[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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

  return (
    <section className="page stack">
      <header className="page-header">
        <div>
          <h1>Agent Runs</h1>
          <p className="muted">Follow bounded build, repair, and test workflows from request to result.</p>
        </div>
      </header>

      {error && <p className="error-text">{error}</p>}
      {isLoading && <p className="muted">Loading agent runs...</p>}
      {!isLoading && runs.length === 0 && (
        <section className="detail-panel">
          <p className="muted">No agent runs yet. Project-mode skill builds and repairs will appear here.</p>
        </section>
      )}

      <div className="item-grid">
        {runs.map((run) => (
          <Link className="item-card" key={run.id} to={`/agent-runs/${run.id}`}>
            <header className="item-card-header">
              <div>
                <h3>Run #{run.id}</h3>
                <p>{run.user_request}</p>
              </div>
              <span className={`badge status-${run.status}`}>{run.status}</span>
            </header>
            <dl className="detail-grid compact-grid">
              <div>
                <dt>Type</dt>
                <dd>{run.run_type}</dd>
              </div>
              <div>
                <dt>Task Node</dt>
                <dd>{run.current_task_id ?? "none"}</dd>
              </div>
              <div>
                <dt>Step</dt>
                <dd>{run.current_step ? formatStepName(run.current_step) : "none"}</dd>
              </div>
              <div>
                <dt>Skill</dt>
                <dd>{run.skill_id ? `#${run.skill_id}` : "none"}</dd>
              </div>
              <div>
                <dt>Created</dt>
                <dd>{formatTimestamp(run.created_at)}</dd>
              </div>
            </dl>
          </Link>
        ))}
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
