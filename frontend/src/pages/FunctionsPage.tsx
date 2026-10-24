import { ReactNode, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { FunctionCatalogEntry, api } from "../api/client";
import { AppIcon } from "../components/AppIcon";
import { RunningStatus } from "../components/RunningStateDot";
import { formatDisplayName } from "../lib/displayName";
import { usePolling } from "../lib/usePolling";
import "./catalog.css";

const categorySourceLabels: Record<FunctionCatalogEntry["category"], string> = {
  backend_core: "Backend core",
  user: "User",
  integration: "Integration",
};

const providerLabels: Record<string, string> = {
  github: "GitHub",
  huggingface: "Hugging Face",
  atlas: "Atlas",
  notion: "Notion",
  google_calendar: "Google Calendar",
  google_drive: "Google Drive",
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
  return (
    <section className="page stack catalog-page">
      <FunctionCatalog />
    </section>
  );
}

export function FunctionCatalog({ afterHeader }: { afterHeader?: ReactNode } = {}) {
  const [functions, setFunctions] = useState<FunctionCatalogEntry[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadFunctions();
  }, []);

  usePolling(() => loadFunctions({ showLoading: false }), true, 5000);

  async function loadFunctions(options: { refresh?: boolean; showLoading?: boolean } = {}) {
    if (options.showLoading !== false) setIsLoading(true);
    setError(null);
    try {
      setFunctions(await api.listFunctionCatalog(options.refresh));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load functions");
    } finally {
      if (options.showLoading !== false) setIsLoading(false);
    }
  }

  return (
    <div className="function-catalog stack">
      <header className="page-header">
        <div>
          <h1>Functions</h1>
        </div>
        <button type="button" className="secondary" onClick={() => void loadFunctions({ refresh: true })} disabled={isLoading}>
          <AppIcon name="refresh" size={15} />Refresh
        </button>
      </header>

      {afterHeader}
      {error && <p className="error-text">{error}</p>}
      {isLoading ? <p className="muted">Loading functions...</p> : <FunctionTable functions={functions} />}
    </div>
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
    <div className="function-catalog catalog-list stack">
      <div className="catalog-toolbar function-filters">
        <label className="catalog-search">
          <AppIcon name="search" size={17} />
          <input
            aria-label="Search"
            type="search"
            value={keyword}
            onChange={(event) => setKeyword(event.target.value)}
            placeholder="Search functions…"
          />
        </label>
        <label className="catalog-select">
          <span>Source</span>
          <select aria-label="Source" value={source} onChange={(event) => setSource(event.target.value)}>
            <option value="all">All sources</option>
            {sourceOptions.map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </label>
        <label className="catalog-select">
          <span>Availability</span>
          <select aria-label="Availability" value={availability} onChange={(event) => setAvailability(event.target.value)}>
            <option value="all">All availability</option>
            <option value="available">Available</option>
            <option value="disabled">Disabled</option>
            <option value="unavailable">Unavailable</option>
            <option value="error">Error</option>
          </select>
        </label>
        <label className="catalog-select">
          <span>Risk</span>
          <select aria-label="Risk" value={risk} onChange={(event) => setRisk(event.target.value)}>
            <option value="all">All risks</option>
            <option value="low">Low</option>
            <option value="medium">Medium</option>
            <option value="high">High</option>
            <option value="blocked">Blocked</option>
          </select>
        </label>
      </div>
      <div className="catalog-list-heading"><span>Function catalog</span><span className="catalog-result-count" aria-live="polite">{filteredFunctions.length} of {functions.length} functions</span></div>
      {filteredFunctions.length === 0 ? <div className="catalog-empty"><div className="catalog-empty-icon"><AppIcon name={functions.length === 0 ? "code" : "search"} size={28} /></div><h2>{functions.length === 0 ? "No functions are registered." : "No functions match these filters."}</h2><p>{functions.length === 0 ? "Platform, integration, and installed skill functions will appear here." : "Try a different keyword or clear your filters to see the full catalog."}</p>{functions.length > 0 && <button className="secondary" type="button" onClick={() => { setKeyword(""); setSource("all"); setAvailability("all"); setRisk("all"); }}>Clear filters</button>}</div> : <div className="table-wrap">
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
                  <span className="table-subtitle">{entry.description}</span>
                  <span className="catalog-function-id">{entry.id}</span>
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
          </tbody>
        </table>
      </div>}
    </div>
  );
}
