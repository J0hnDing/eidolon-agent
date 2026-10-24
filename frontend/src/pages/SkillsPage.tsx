import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { Skill, api } from "../api/client";
import { AppIcon } from "../components/AppIcon";
import { RunningStatus } from "../components/RunningStateDot";
import { formatDisplayName } from "../lib/displayName";
import { usePolling } from "../lib/usePolling";
import "./catalog.css";

export default function SkillsPage() {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [keyword, setKeyword] = useState("");
  const [runtime, setRuntime] = useState("all");
  const [availability, setAvailability] = useState("all");
  const filteredSkills = skills.filter((skill) =>
    (runtime === "all" || skill.runtime === runtime)
    && (availability === "all" || (availability === "enabled" ? skill.enabled : !skill.enabled))
    && `${skill.name} ${formatDisplayName(skill.name)} ${skill.description}`.toLocaleLowerCase().includes(keyword.trim().toLocaleLowerCase()),
  );

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
    <section className="page stack catalog-page skills-page">
      <header className="page-header">
        <div>
          <h1>Skills</h1>
        </div>
        <div className="button-row">
          <Link className="button-link" to="/skills/build"><AppIcon name="plus" size={16} />Build a skill</Link>
          <button type="button" className="secondary" onClick={() => void loadSkills()} disabled={isLoading}>
            <AppIcon name="refresh" size={15} />Refresh
          </button>
        </div>
      </header>

      <div className="catalog-tabs" aria-label="Skill runtime filters">
        {[["all", "All skills"], ["function", "Functions"], ["web_app", "Applications"], ["service", "Services"]].map(([value, label]) => (
          <button className={runtime === value ? "catalog-tab is-active" : "catalog-tab"} type="button" key={value} aria-pressed={runtime === value} onClick={() => setRuntime(value)}>
            {label}<span>{value === "all" ? skills.length : skills.filter((skill) => skill.runtime === value).length}</span>
          </button>
        ))}
      </div>
      <div className="catalog-toolbar">
        <label className="catalog-search"><AppIcon name="search" size={17} /><input type="search" aria-label="Search skills" placeholder="Search skills…" value={keyword} onChange={(event) => setKeyword(event.target.value)} /></label>
        <label className="catalog-select"><span>Availability</span><select aria-label="Availability" value={availability} onChange={(event) => setAvailability(event.target.value)}><option value="all">All skills</option><option value="enabled">Enabled</option><option value="disabled">Disabled</option></select></label>
        <span className="catalog-result-count" aria-live="polite">{filteredSkills.length} {filteredSkills.length === 1 ? "skill" : "skills"}</span>
      </div>

      {error && <p className="error-text">{error}</p>}
      {isLoading ? (
        <p className="muted">Loading skills...</p>
      ) : (
        skills.length === 0 ? <div className="catalog-empty"><div className="catalog-empty-icon"><AppIcon name="skills" size={30} /></div><h2>A home for your capabilities</h2><p>No skills yet. Build one to add a reusable capability.</p><p>Describe what you need in Build a skill. You review the result before installation.</p></div>
        : filteredSkills.length === 0 ? <div className="catalog-empty"><AppIcon name="search" size={28} /><h2>No skills match these filters</h2><p>Try another search or show all your skills.</p><button className="secondary" type="button" onClick={() => { setKeyword(""); setRuntime("all"); setAvailability("all"); }}>Clear filters</button></div>
        : <SkillTable skills={filteredSkills} />
      )}
    </section>
  );
}

export function SkillTable({ skills }: { skills: Skill[] }) {
  const navigate = useNavigate();

  return (
    <section className="skill-section catalog-list">
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
                    <span className="catalog-row-icon"><AppIcon name={skill.runtime === "web_app" ? "apps" : skill.runtime === "service" ? "clock" : "code"} size={18} /></span>
                    <strong className="clickable-row-title">
                      {formatDisplayName(skill.name)}
                      <span className="row-reveal-arrow" aria-hidden="true">→</span>
                    </strong>
                  </span>
                  <span className="table-subtitle">{skill.description}</span>
                </td>
                <td>
                  {skill.is_running ? (
                    <RunningStatus />
                  ) : (
                    <span className={`badge status-${skill.status}`}>{skill.status}</span>
                  )}
                </td>
                <td><span className="catalog-runtime">{skill.runtime === "web_app" ? "Application" : skill.runtime === "service" ? "Service" : "Function"}</span></td>
                <td>
                  <span className={`badge risk-${skill.risk_level}`}>{skill.risk_level}</span>
                </td>
                <td>{skill.enabled ? "enabled" : "disabled"}</td>
              </tr>
            ))}
            {skills.length === 0 && (
              <tr>
                <td colSpan={5} className="muted">
                  No skills yet. Build one to add a reusable capability.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
