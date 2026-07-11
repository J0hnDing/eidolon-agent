import { useEffect, useState } from "react";

import { CodexAccountUsage, CodexUsageWindow, api } from "../api/client";

export default function UsageSettingsPage() {
  const [usage, setUsage] = useState<CodexAccountUsage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  async function loadUsage() {
    setError(null);
    try {
      setUsage(await api.getCodexUsage());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load Codex usage");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadUsage();
  }, []);

  return (
    <section className="page stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">Settings</p>
          <h1>Codex Usage</h1>
          <p className="muted">Live account allowance from the local Codex App Server. DAG builds pause before the next ready batch when either window has less than 5% remaining.</p>
        </div>
        <button type="button" className="secondary" onClick={() => void loadUsage()} disabled={loading}>
          Refresh
        </button>
      </header>
      {error && <p className="error-text">{error}</p>}
      {usage && !usage.available && <p className="error-text">{usage.error ?? "Codex usage is unavailable."}</p>}
      {usage?.available && (
        <>
          <section className="detail-panel">
            <dl className="detail-grid">
              <div><dt>Plan</dt><dd>{usage.plan_type ?? "unknown"}</dd></div>
              <div><dt>Limit</dt><dd>{usage.limit_id}</dd></div>
              <div><dt>Updated</dt><dd>{formatDate(usage.fetched_at)}</dd></div>
            </dl>
          </section>
          <div className="section-grid">
            <UsageCard title="5-hour allowance" window={usage.five_hour} />
            <UsageCard title="Weekly allowance" window={usage.weekly} />
          </div>
        </>
      )}
      <p className="muted">Skill runtime calls are intentionally excluded from build token accounting.</p>
    </section>
  );
}

function UsageCard({ title, window }: { title: string; window: CodexUsageWindow | null }) {
  if (!window) return <section className="detail-panel"><h2>{title}</h2><p className="muted">Unavailable</p></section>;
  return (
    <section className="detail-panel">
      <h2>{title}</h2>
      <p><strong>{window.remaining_percent}% remaining</strong></p>
      <progress max={100} value={window.remaining_percent} aria-label={`${title} remaining`} />
      <p className="muted">{window.used_percent}% used · resets {formatDate(window.resets_at)}</p>
    </section>
  );
}

function formatDate(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "unknown";
}
