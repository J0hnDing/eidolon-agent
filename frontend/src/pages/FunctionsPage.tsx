import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { FunctionCatalogEntry, api } from "../api/client";
import { RunningStatus } from "../components/RunningStateDot";
import { formatDisplayName } from "../lib/displayName";
import { usePolling } from "../lib/usePolling";

const categorySourceLabels: Record<FunctionCatalogEntry["category"], string> = {
  backend_core: "Backend core",
  user: "User",
  integration: "Integration",
};

const providerLabels: Record<string, string> = {
  github: "GitHub",
  atlas: "Atlas",
  notion: "Notion",
  google_calendar: "Google Calendar",
  gmail: "Gmail",
  telegram: "Telegram",
};

function sourceKey(entry: FunctionCatalogEntry): string {
  return entry.category === "integration" && entry.provider
    ? `provider:${entry.provider}`
    : `category:${entry.category}`;
}

export function functionSourceLabel(entry: FunctionCatalogEntry): string {
  if (entry.category !== "integration" || !entry.provider) {
    return categorySourceLabels[entry.category];
  }
  return providerLabels[entry.provider] ?? formatDisplayName(entry.provider);
}

export default function FunctionsPage() {
  const [functions, setFunctions] = useState<FunctionCatalogEntry[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadFunctions();
  }, []);

  usePolling(() => loadFunctions({ showLoading: false }), true, 5000);

  async function loadFunctions(options: { showLoading?: boolean } = {}) {
    if (options.showLoading !== false) setIsLoading(true);
    setError(null);
    try {
      setFunctions(await api.listFunctionCatalog());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load functions");
    } finally {
      if (options.showLoading !== false) setIsLoading(false);
    }
  }

  return (
    <section className="page stack">
      <header className="page-header">
        <div>
          <h1>Functions</h1>
          <p className="muted">
            Backend, user, and integration functions available to application skills.
          </p>
        </div>
      </header>

      {error && <p className="error-text">{error}</p>}
      {isLoading ? <p className="muted">Loading functions...</p> : <FunctionTable functions={functions} />}
    </section>
  );
}

export function FunctionTable({ functions }: { functions: FunctionCatalogEntry[] }) {
  const [keyword, setKeyword] = useState("");
  const [source, setSource] = useState("all");
  const [availability, setAvailability] = useState("all");
  const [risk, setRisk] = useState("all");

  const sourceOptions = Array.from(
    new Map(functions.map((entry) => [sourceKey(entry), functionSourceLabel(entry)])).entries(),
  ).sort((left, right) => left[1].localeCompare(right[1]));
  const normalizedKeyword = keyword.trim().toLocaleLowerCase();
  const filteredFunctions = functions.filter((entry) => {
    const matchesKeyword = !normalizedKeyword || [
      entry.title,
      entry.id,
      entry.description,
      functionSourceLabel(entry),
    ].some((value) => value.toLocaleLowerCase().includes(normalizedKeyword));
    return matchesKeyword
      && (source === "all" || sourceKey(entry) === source)
      && (availability === "all" || entry.availability === availability)
      && (risk === "all" || entry.risk_level === risk);
  });

  return (
    <div className="stack">
      <div className="function-filters">
        <label>
          Search
          <input
            type="search"
            value={keyword}
            onChange={(event) => setKeyword(event.target.value)}
            placeholder="Search functions"
          />
        </label>
        <label>
          Source
          <select value={source} onChange={(event) => setSource(event.target.value)}>
            <option value="all">All sources</option>
            {sourceOptions.map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </label>
        <label>
          Availability
          <select value={availability} onChange={(event) => setAvailability(event.target.value)}>
            <option value="all">All availability</option>
            <option value="available">Available</option>
            <option value="disabled">Disabled</option>
            <option value="unavailable">Unavailable</option>
            <option value="error">Error</option>
          </select>
        </label>
        <label>
          Risk
          <select value={risk} onChange={(event) => setRisk(event.target.value)}>
            <option value="all">All risks</option>
            <option value="low">Low</option>
            <option value="medium">Medium</option>
            <option value="high">High</option>
            <option value="blocked">Blocked</option>
          </select>
        </label>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Function</th>
              <th>Source</th>
              <th>Availability</th>
              <th>Risk</th>
              <th>Version</th>
            </tr>
          </thead>
          <tbody>
            {filteredFunctions.map((entry) => (
              <tr key={entry.id}>
                <td>
                  <span className="entry-title-line">
                    <strong>
                      {entry.skill_id ? (
                        <Link to={`/skills/${entry.skill_id}`}>{formatDisplayName(entry.title)}</Link>
                      ) : formatDisplayName(entry.title)}
                    </strong>
                  </span>
                  <span className="table-subtitle">{entry.id}</span>
                  <span className="table-subtitle">{entry.description}</span>
                </td>
                <td>{functionSourceLabel(entry)}</td>
                <td>
                  {entry.is_running ? (
                    <RunningStatus />
                  ) : (
                    <span className={`badge status-${entry.availability}`}>{entry.availability}</span>
                  )}
                  {entry.availability_reasons.map((reason) => (
                    <span className="table-subtitle" key={reason}>{reason}</span>
                  ))}
                </td>
                <td>
                  <span className={`badge risk-${entry.risk_level}`}>{entry.risk_level}</span>
                </td>
                <td>{entry.active_version ?? "platform"}</td>
              </tr>
            ))}
            {filteredFunctions.length === 0 && (
              <tr>
                <td colSpan={5} className="muted">
                  {functions.length === 0 ? "No functions are registered." : "No functions match these filters."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
