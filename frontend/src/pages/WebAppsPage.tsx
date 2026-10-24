import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { Skill, api } from "../api/client";
import { AppIcon } from "../components/AppIcon";
import { RunningStatus } from "../components/RunningStateDot";
import { formatDisplayName } from "../lib/displayName";
import { usePolling } from "../lib/usePolling";
import "./catalog.css";

export function applicationSkills(skills: Skill[]) {
  return skills.filter((skill) => skill.runtime === "web_app");
}

export default function WebAppsPage() {
  const [apps, setApps] = useState<Skill[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [keyword, setKeyword] = useState("");
  const [availability, setAvailability] = useState("all");
  const filteredApps = apps.filter((skill) =>
    `${skill.name} ${formatDisplayName(skill.name)} ${skill.description}`.toLocaleLowerCase().includes(keyword.trim().toLocaleLowerCase())
    && (availability === "all" || (availability === "available" ? skill.status === "installed" && skill.enabled : skill.is_running)),
  );

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
    <section className="page stack catalog-page applications-page">
      <header className="page-header">
        <div>
          <h1>Applications</h1>
        </div>
        <div className="button-row"><Link className="button-link secondary" to="/skills">Manage skills<AppIcon name="arrow" size={15} /></Link><button type="button" className="secondary" onClick={() => void loadApps()} disabled={isLoading}><AppIcon name="refresh" size={15} />Refresh</button></div>
      </header>
      <div className="catalog-toolbar">
        <label className="catalog-search"><AppIcon name="search" size={17} /><input aria-label="Search applications" type="search" placeholder="Search applications…" value={keyword} onChange={(event) => setKeyword(event.target.value)} /></label>
        <label className="catalog-select"><span>Show</span><select aria-label="Show" value={availability} onChange={(event) => setAvailability(event.target.value)}><option value="all">All applications</option><option value="available">Ready to open</option><option value="running">Running</option></select></label>
        <span className="catalog-result-count" aria-live="polite">{filteredApps.length} {filteredApps.length === 1 ? "application" : "applications"}</span>
      </div>
      {error && <p className="error-text">{error}</p>}
      {isLoading ? (
        <p className="muted">Loading applications...</p>
      ) : (
        <div className="application-grid">
          {filteredApps.map((skill) => (
            <article className="application-card" key={skill.id}>
              <div className="application-card-top">
                <span className="application-icon"><AppIcon name="apps" size={24} /></span>
                {skill.is_running ? <RunningStatus /> : <span className={`badge status-${skill.status}`}>{skill.status}</span>}
              </div>
              <div className="application-card-content"><h2>{formatDisplayName(skill.name)}</h2><p>{skill.description || "A personal web application."}</p></div>
              <div className="application-card-footer">
                {skill.status === "installed" && skill.enabled && (
                  <Link className="application-open-link" to={`/apps/${skill.id}`}>Open Application<AppIcon name="arrow" size={15} /></Link>
                )}
                {!(skill.status === "installed" && skill.enabled) && <span className="catalog-secondary">{skill.enabled ? "Awaiting installation" : "Disabled"}</span>}
                <Link className="application-details-link" to={`/skills/${skill.id}`} aria-label={`Open ${formatDisplayName(skill.name)} skill details`}>Details<AppIcon name="chevron" size={14} /></Link>
              </div>
            </article>
          ))}
          {apps.length === 0 && (
            <section className="catalog-empty" aria-labelledby="applications-empty-title">
              <div className="catalog-empty-icon"><AppIcon name="apps" size={30} /></div>
              <div>
                <h2 id="applications-empty-title">No applications yet</h2>
                <p className="muted">Install a web application skill to make it available here.</p>
              </div>
              <div className="button-row">
                <Link className="button-link" to="/skills">Browse skills</Link>
              </div>
            </section>
          )}
          {apps.length > 0 && filteredApps.length === 0 && <div className="catalog-empty"><AppIcon name="search" size={28} /><h2>No applications match these filters</h2><p>Try a different search or show all applications.</p><button type="button" className="secondary" onClick={() => { setKeyword(""); setAvailability("all"); }}>Clear filters</button></div>}
        </div>
      )}
    </section>
  );
}
