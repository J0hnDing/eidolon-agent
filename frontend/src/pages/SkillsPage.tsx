import { FormEvent, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { RiskLevel, Skill, SkillInput, SkillStatus, SkillType, api } from "../api/client";

const skillStatuses: SkillStatus[] = ["proposed", "installed", "disabled", "failed", "deleted"];
const riskLevels: RiskLevel[] = ["low", "medium", "high"];
const skillTypes: SkillType[] = ["instruction", "automation", "hybrid"];

const emptySkillForm: SkillInput = {
  name: "",
  description: "",
  skill_type: "automation",
  status: "proposed",
  risk_level: "low",
  manifest_path: "",
  instructions_path: null,
  installed_path: null,
  enabled: false,
};

export default function SkillsPage() {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [form, setForm] = useState<SkillInput>(emptySkillForm);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadSkills();
  }, []);

  async function loadSkills() {
    setIsLoading(true);
    setError(null);
    try {
      setSkills(await api.listSkills());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load skills");
    } finally {
      setIsLoading(false);
    }
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    try {
      await api.createSkill({
        ...form,
        manifest_path:
          form.manifest_path.trim() || `skills/proposed/${form.name}/manifest.json`,
        installed_path: form.installed_path?.trim() || null,
        instructions_path: form.instructions_path?.trim() || null,
      });
      setForm(emptySkillForm);
      await loadSkills();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create skill");
    }
  }

  return (
    <section className="page stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">Reusable automations</p>
          <h1>Skills</h1>
        </div>
      </header>

      <form className="form-panel" onSubmit={handleSubmit}>
        <h2>Create Skill Record</h2>
        <div className="form-grid">
          <label>
            Name
            <input
              value={form.name}
              onChange={(event) =>
                setForm({
                  ...form,
                  name: event.target.value,
                  manifest_path: `skills/proposed/${event.target.value}/manifest.json`,
                })
              }
              placeholder="ai_news_digest"
              required
              pattern="^[a-z][a-z0-9_]*$"
            />
          </label>
          <label>
            Skill Type
            <select
              value={form.skill_type}
              onChange={(event) => setForm({ ...form, skill_type: event.target.value as SkillType })}
            >
              {skillTypes.map((skillType) => (
                <option key={skillType} value={skillType}>
                  {skillType}
                </option>
              ))}
            </select>
          </label>
          <label>
            Status
            <select
              value={form.status}
              onChange={(event) => setForm({ ...form, status: event.target.value as SkillStatus })}
            >
              {skillStatuses.map((status) => (
                <option key={status} value={status}>
                  {status}
                </option>
              ))}
            </select>
          </label>
          <label>
            Risk Level
            <select
              value={form.risk_level}
              onChange={(event) => setForm({ ...form, risk_level: event.target.value as RiskLevel })}
            >
              {riskLevels.map((risk) => (
                <option key={risk} value={risk}>
                  {risk}
                </option>
              ))}
            </select>
          </label>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={form.enabled}
              onChange={(event) => setForm({ ...form, enabled: event.target.checked })}
            />
            Enabled
          </label>
        </div>
        <label>
          Description
          <textarea
            value={form.description}
            onChange={(event) => setForm({ ...form, description: event.target.value })}
            required
            rows={3}
          />
        </label>
        <div className="form-grid">
          <label>
            Manifest Path
            <input
              value={form.manifest_path}
              onChange={(event) => setForm({ ...form, manifest_path: event.target.value })}
              required
            />
          </label>
          <label>
            Instructions Path
            <input
              value={form.instructions_path ?? ""}
              onChange={(event) =>
                setForm({ ...form, instructions_path: event.target.value || null })
              }
            />
          </label>
          <label>
            Installed Path
            <input
              value={form.installed_path ?? ""}
              onChange={(event) =>
                setForm({ ...form, installed_path: event.target.value || null })
              }
            />
          </label>
        </div>
        <button type="submit">Create Skill</button>
      </form>

      {error && <p className="error-text">{error}</p>}
      {isLoading ? (
        <p className="muted">Loading skills...</p>
      ) : (
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
                    <span className="badge">{skill.status}</span>
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
                    No skill records yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
