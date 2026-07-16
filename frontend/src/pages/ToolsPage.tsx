import { Link } from "react-router-dom";
import { useEffect, useState } from "react";

import { Tool, api } from "../api/client";
import { usePolling } from "../lib/usePolling";

export default function ToolsPage() {
  const [tools, setTools] = useState<Tool[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadTools();
  }, []);

  usePolling(() => loadTools({ showLoading: false }), true, 5000);

  async function loadTools(options: { showLoading?: boolean } = {}) {
    if (options.showLoading !== false) setIsLoading(true);
    setError(null);
    try {
      setTools(await api.listTools());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load tools");
    } finally {
      if (options.showLoading !== false) setIsLoading(false);
    }
  }

  return (
    <section className="page stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">Manual Interfaces</p>
          <h1>Tools</h1>
        </div>
      </header>

      {error && <p className="error-text">{error}</p>}
      {isLoading && <p className="muted">Loading tools...</p>}
      {!isLoading && tools.length === 0 && (
        <section className="detail-panel">
          <p className="muted">
            No installed enabled tool skills are available. Tool skills are still application skills; they use
            interface_type = tool.
          </p>
        </section>
      )}

      <div className="tool-card-grid">
        {tools.map((tool) => {
          const title = getUiText(tool.skill.tool_ui_schema_json, "title") ?? tool.skill.name;
          const description =
            getUiText(tool.skill.tool_ui_schema_json, "description") ?? tool.skill.description;
          const isReady = tool.runtime_permission_status === "ready";
          return (
            <Link className="tool-card" key={tool.skill.id} to={`/tools/${tool.skill.id}`}>
              <div className="tool-card-header">
                <div>
                  <h2>{title}</h2>
                  <p>{description}</p>
                </div>
                <span className={`status-badge ${isReady ? "installed" : "failed"}`}>
                  {tool.runtime_permission_status}
                </span>
              </div>
              <dl className="tool-card-meta">
                <div>
                  <dt>Risk</dt>
                  <dd>{tool.skill.risk_level}</dd>
                </div>
              </dl>
            </Link>
          );
        })}
      </div>
    </section>
  );
}

function getUiText(schema: Record<string, unknown> | null, key: string): string | null {
  const value = schema?.[key];
  return typeof value === "string" && value.trim() ? value : null;
}
