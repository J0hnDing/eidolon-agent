import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { Skill, api } from "../api/client";

const placeholderSections = [
  "Manifest",
  "Permissions",
  "Test Results",
  "Run History",
  "Logs",
  "Actions",
];

export default function SkillDetailPage() {
  const { skillId } = useParams();
  const [skill, setSkill] = useState<Skill | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function loadSkill() {
      if (!skillId) return;
      setIsLoading(true);
      setError(null);
      try {
        setSkill(await api.getSkill(Number(skillId)));
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not load skill");
      } finally {
        setIsLoading(false);
      }
    }
    loadSkill();
  }, [skillId]);

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
            <dt>Installed Path</dt>
            <dd>{skill.installed_path ?? "not installed"}</dd>
          </div>
        </dl>
      </section>

      <div className="button-row">
        <button type="button" disabled>
          Run
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
