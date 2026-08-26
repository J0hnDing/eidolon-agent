import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { Skill, api } from "../api/client";
import { usePolling } from "../lib/usePolling";

export function applicationSkills(skills: Skill[]) {
  return skills.filter((skill) => skill.runtime === "web_app");
}

export default function WebAppsPage() {
  const [apps, setApps] = useState<Skill[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadApps();
  }, []);

  usePolling(() => loadApps(false), true, 5000);

  async function loadApps(showLoading = true) {
    if (showLoading) setIsLoading(true);
    setError(null);
    try {
      setApps(applicationSkills(await api.listSkills()));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load web applications");
    } finally {
      if (showLoading) setIsLoading(false);
    }
  }

  return (
    <section className="page stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">Self-rendered sandboxed skills</p>
          <h1>Applications</h1>
          <p className="muted">
            Application content runs on an isolated origin while Eidolon keeps lifecycle, version,
            permission, and sandbox controls outside the frame.
          </p>
        </div>
      </header>
      {error && <p className="error-text">{error}</p>}
      {isLoading ? (
        <p className="muted">Loading applications...</p>
      ) : (
        <div className="item-grid">
          {apps.map((skill) => (
            <article className="item-card" key={skill.id}>
              <div className="item-card-header">
                <div>
                  <h3>{skill.name}</h3>
                  <p>{skill.description}</p>
                </div>
                <span className={`badge status-${skill.status}`}>{skill.status}</span>
              </div>
              <p className="muted">{skill.enabled ? "Enabled and available for lazy startup." : "Disabled."}</p>
              <div className="button-row">
                {skill.status === "installed" && skill.enabled ? (
                  <Link className="button-link" to={`/apps/${skill.id}`}>Open Application</Link>
                ) : (
                  <Link to={`/skills/${skill.id}`}>Review skill</Link>
                )}
              </div>
            </article>
          ))}
          {apps.length === 0 && <p className="muted">No web application skills are installed yet.</p>}
        </div>
      )}
    </section>
  );
}
