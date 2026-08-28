import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { Skill, api } from "../api/client";
import { usePolling } from "../lib/usePolling";

export default function SkillsPage() {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadSkills();
  }, []);

  usePolling(() => loadSkills({ showLoading: false }), true, 5000);

  async function loadSkills(options: { showLoading?: boolean } = {}) {
    if (options.showLoading !== false) setIsLoading(true);
    setError(null);
    try {
      setSkills(await api.listSkills());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load skills");
    } finally {
      if (options.showLoading !== false) setIsLoading(false);
    }
  }

  return (
    <section className="page stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">Reusable capability packages</p>
          <h1>Skills</h1>
          <p className="muted">Inspect, validate, and manage every capability Eidolon can run.</p>
        </div>
      </header>

      {error && <p className="error-text">{error}</p>}
      {isLoading ? (
        <p className="muted">Loading skills...</p>
      ) : (
        <SkillTable skills={skills} />
      )}
    </section>
  );
}

function SkillTable({ skills }: { skills: Skill[] }) {
  return (
    <section className="skill-section">
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Status</th>
              <th>Runtime</th>
              <th>Risk</th>
              <th>Availability</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {skills.map((skill) => (
              <tr key={skill.id}>
                <td>
                  <strong>{skill.name}</strong>
                  <span className="table-subtitle">{skill.description}</span>
                </td>
                <td>
                  <span className={`badge status-${skill.status}`}>{skill.status}</span>
                </td>
                <td>{skill.runtime}</td>
                <td>
                  <span className={`badge risk-${skill.risk_level}`}>{skill.risk_level}</span>
                </td>
                <td>{skill.runtime === "service" ? "schedule-managed" : skill.enabled ? "enabled" : "disabled"}</td>
                <td>
                  {skill.runtime === "web_app" && skill.status === "installed" && skill.enabled ? (
                    <Link to={`/apps/${skill.id}`}>Open App</Link>
                  ) : (
                    <Link to={`/skills/${skill.id}`}>Open</Link>
                  )}
                </td>
              </tr>
            ))}
            {skills.length === 0 && (
              <tr>
                <td colSpan={6} className="muted">
                  Nothing here yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
