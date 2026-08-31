import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { FunctionCatalogEntry, api } from "../api/client";
import { formatDisplayName } from "../lib/displayName";
import { usePolling } from "../lib/usePolling";

const categoryLabels: Record<FunctionCatalogEntry["category"], string> = {
  backend_core: "Backend core",
  user: "User",
  integration: "Integration",
};

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
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Function</th>
            <th>Category</th>
            <th>Availability</th>
            <th>Risk</th>
            <th>Version</th>
          </tr>
        </thead>
        <tbody>
          {functions.map((entry) => (
            <tr key={entry.id}>
              <td>
                <strong>
                  {entry.skill_id ? (
                    <Link to={`/skills/${entry.skill_id}`}>{formatDisplayName(entry.title)}</Link>
                  ) : formatDisplayName(entry.title)}
                </strong>
                <span className="table-subtitle">{entry.id}</span>
                <span className="table-subtitle">{entry.description}</span>
              </td>
              <td>{categoryLabels[entry.category]}</td>
              <td>
                <span className={`badge status-${entry.availability}`}>{entry.availability}</span>
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
          {functions.length === 0 && (
            <tr>
              <td colSpan={5} className="muted">No functions are registered.</td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
