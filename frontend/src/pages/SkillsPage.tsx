import { FormEvent, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { Skill, SkillType, api } from "../api/client";
import { usePolling } from "../lib/usePolling";

const skillTypes: SkillType[] = ["instruction", "automation"];

export default function SkillsPage() {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [sampleName, setSampleName] = useState("sample_echo_skill");
  const [sampleType, setSampleType] = useState<SkillType>("automation");
  const [isLoading, setIsLoading] = useState(true);
  const [isCreatingSample, setIsCreatingSample] = useState(false);
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

  async function handleCreateSample(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setIsCreatingSample(true);
    setError(null);
    try {
      await api.createSampleProposedSkill({
        name: sampleName.trim(),
        skill_type: sampleType,
      });
      await loadSkills();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create sample proposed skill");
    } finally {
      setIsCreatingSample(false);
    }
  }

  return (
    <section className="page stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">Reusable capability packages</p>
          <h1>Skills</h1>
        </div>
      </header>

      <form className="form-panel" onSubmit={handleCreateSample}>
        <h2>Create Sample Proposed Skill</h2>
        <p className="muted">
          Use these samples to test the proposed skill workflow. Real Codex-generated proposed
          skills will be introduced in Milestone 6.
        </p>
        <div className="form-grid">
          <label>
            Name
            <input
              value={sampleName}
              onChange={(event) => setSampleName(event.target.value)}
              required
              pattern="^[a-zA-Z0-9_-]+$"
            />
          </label>
          <label>
            Skill Type
            <select
              value={sampleType}
              onChange={(event) => setSampleType(event.target.value as SkillType)}
            >
              {skillTypes.map((skillType) => (
                <option key={skillType} value={skillType}>
                  {skillType}
                </option>
              ))}
            </select>
          </label>
        </div>
        <button type="submit" disabled={isCreatingSample}>
          {isCreatingSample ? "Creating..." : "Create Sample Proposed Skill"}
        </button>
      </form>

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
              <th>Type</th>
              <th>Risk</th>
              <th>Enabled</th>
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
                <td>
                  <span className="badge">{skill.skill_type}</span>
                </td>
                <td>
                  <span className={`badge risk-${skill.risk_level}`}>{skill.risk_level}</span>
                </td>
                <td>{skill.enabled ? "enabled" : "disabled"}</td>
                <td>
                  <Link to={`/skills/${skill.id}`}>Open</Link>
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
