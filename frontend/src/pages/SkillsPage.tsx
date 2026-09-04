import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { Skill, api } from "../api/client";
import RunningStateDot from "../components/RunningStateDot";
import { formatDisplayName } from "../lib/displayName";
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

export function SkillTable({ skills }: { skills: Skill[] }) {
  const navigate = useNavigate();

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
            </tr>
          </thead>
          <tbody>
            {skills.map((skill) => (
              <tr
                className="clickable-table-row"
                key={skill.id}
                role="link"
                tabIndex={0}
                aria-label={`Open ${formatDisplayName(skill.name)} skill details`}
                onClick={() => navigate(`/skills/${skill.id}`)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") navigate(`/skills/${skill.id}`);
                }}
              >
                <td>
                  <span className="entry-title-line">
                    <span className="running-state-slot">
                      {skill.is_running && <RunningStateDot />}
                    </span>
                    <strong className="clickable-row-title">
                      {formatDisplayName(skill.name)}
                      <span className="row-reveal-arrow" aria-hidden="true">→</span>
                    </strong>
                  </span>
                  <span className="table-subtitle">{skill.description}</span>
                </td>
                <td>
                  <span className={`badge status-${skill.status}`}>{skill.status}</span>
                </td>
                <td>{skill.runtime}</td>
                <td>
                  <span className={`badge risk-${skill.risk_level}`}>{skill.risk_level}</span>
                </td>
                <td>{skill.enabled ? "enabled" : "disabled"}</td>
              </tr>
            ))}
            {skills.length === 0 && (
              <tr>
                <td colSpan={5} className="muted">
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
