import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { Skill, SkillRun, api } from "../api/client";

const placeholderSections = [
  "Manifest",
  "Permissions",
  "Test Results",
  "Actions",
];

export default function SkillDetailPage() {
  const { skillId } = useParams();
  const [skill, setSkill] = useState<Skill | null>(null);
  const [runs, setRuns] = useState<SkillRun[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isRunning, setIsRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const latestRun = runs[0] ?? null;

  useEffect(() => {
    async function loadSkillDetail() {
      if (!skillId) return;
      setIsLoading(true);
      setError(null);
      try {
        const id = Number(skillId);
        const [loadedSkill, loadedRuns] = await Promise.all([
          api.getSkill(id),
          api.listSkillRuns(id),
        ]);
        setSkill(loadedSkill);
        setRuns(loadedRuns);
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
    setIsRunning(true);
    setError(null);
    try {
      const run = await api.runSkill(skill.id, {});
      setRuns((current) => [run, ...current.filter((item) => item.id !== run.id)]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not run skill");
    } finally {
      setIsRunning(false);
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
        </dl>
      </section>

      <div className="button-row">
        <button type="button" onClick={handleRun} disabled={isRunning}>
          {isRunning ? "Running..." : "Run"}
        </button>
        <button type="button" disabled>
          Disable
        </button>
        <button type="button" className="danger" disabled>
          Delete
        </button>
        <button type="button" disabled>
          View Code
        </button>
        <button type="button" disabled>
          View Logs
        </button>
      </div>

      {error && <p className="error-text">{error}</p>}

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
                  <span>{run.started_at ? new Date(run.started_at).toLocaleString() : "not started"}</span>
                </div>
                <span className={`badge run-${run.status}`}>{run.status}</span>
              </article>
            ))}
          </div>
        ) : (
          <p className="muted">No run history yet.</p>
        )}
      </section>

      <div className="section-grid">
        {placeholderSections.map((section) => (
          <section key={section} className="placeholder-section">
            <h2>{section}</h2>
            <p>This area will be connected in a later milestone.</p>
          </section>
        ))}
      </div>
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
          <dd>{run.started_at ? new Date(run.started_at).toLocaleString() : "not started"}</dd>
        </div>
        <div>
          <dt>Ended</dt>
          <dd>{run.ended_at ? new Date(run.ended_at).toLocaleString() : "not ended"}</dd>
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
