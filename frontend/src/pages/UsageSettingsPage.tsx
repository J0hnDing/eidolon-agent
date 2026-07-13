import { useEffect, useState } from "react";

import {
  CodexAccountUsage,
  CodexCliStatus,
  CodexInvocationChoice,
  CodexModelCatalog,
  CodexRoutingSettings,
  CodexRoutingSettingsPayload,
  CodexUsageWindow,
  api,
} from "../api/client";

export default function UsageSettingsPage() {
  const [usage, setUsage] = useState<CodexAccountUsage | null>(null);
  const [cliStatus, setCliStatus] = useState<CodexCliStatus | null>(null);
  const [catalog, setCatalog] = useState<CodexModelCatalog | null>(null);
  const [routing, setRouting] = useState<CodexRoutingSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  async function loadUsage(refresh = false) {
    setError(null);
    try {
      const [nextUsage, nextCliStatus, nextCatalog, nextRouting] = await Promise.all([
        api.getCodexUsage(),
        api.getCodexCliStatus(refresh),
        api.getCodexModels(refresh),
        api.getCodexRoutingSettings(),
      ]);
      setUsage(nextUsage);
      setCliStatus(nextCliStatus);
      setCatalog(nextCatalog);
      setRouting(nextRouting);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load Codex usage");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadUsage();
  }, []);

  function updateChoice(
    group: "chat" | "product_manager" | "builder" | "tester",
    key: string,
    choice: CodexInvocationChoice,
  ) {
    setSaved(null);
    setRouting((current) => {
      if (!current) return current;
      if (group === "chat") return { ...current, chat: choice };
      const nextGroup = { ...current[group], [key]: choice };
      return { ...current, [group]: nextGroup };
    });
  }

  async function saveRouting() {
    if (!routing) return;
    setError(null);
    setSaved(null);
    setLoading(true);
    try {
      const { updated_at: _updatedAt, ...payload } = routing;
      const next = await api.updateCodexRoutingSettings(payload as CodexRoutingSettingsPayload);
      setRouting(next);
      setSaved("Model routing settings saved.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save model routing settings");
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="page stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">Settings</p>
          <h1>Codex Settings</h1>
          <p className="muted">Live account allowance from the local Codex App Server. DAG builds pause before the next ready batch when either window has less than 5% remaining.</p>
        </div>
        <button type="button" className="secondary" onClick={() => void loadUsage(true)} disabled={loading}>
          Refresh
        </button>
      </header>
      {error && <p className="error-text">{error}</p>}
      {saved && <p className="success-text">{saved}</p>}
      {catalog && !catalog.available && (
        <p className="error-text">Model choices are unavailable: {catalog.error ?? "Codex model catalog could not be loaded."}</p>
      )}
      {routing && catalog?.available && (
        <>
          <section className="detail-panel stack">
            <div>
              <h2>Chat routing</h2>
              <p className="muted">Normal Chat mode is independent from Project workflow routing.</p>
            </div>
            <RoutingRow
              label="Chat"
              choice={routing.chat}
              models={catalog.models}
              onChange={(choice) => updateChoice("chat", "chat", choice)}
            />
          </section>

          <section className="detail-panel stack">
            <div>
              <h2>ProductManager routing</h2>
              <p className="muted">Action settings inherit any model or effort left blank from the ProductManager default.</p>
            </div>
            <RoutingRow label="Default" choice={routing.product_manager.default} models={catalog.models} onChange={(choice) => updateChoice("product_manager", "default", choice)} />
            <RoutingRow label="Refine intent" choice={routing.product_manager.refine_intent} fallback={routing.product_manager.default} models={catalog.models} onChange={(choice) => updateChoice("product_manager", "refine_intent", choice)} />
            <RoutingRow label="Plausibility review" choice={routing.product_manager.plausibility_review} fallback={routing.product_manager.default} models={catalog.models} onChange={(choice) => updateChoice("product_manager", "plausibility_review", choice)} />
            <RoutingRow label="Blueprint and permissions" choice={routing.product_manager.blueprint_and_permissions} fallback={routing.product_manager.default} models={catalog.models} onChange={(choice) => updateChoice("product_manager", "blueprint_and_permissions", choice)} />
            <RoutingRow label="Task DAG" choice={routing.product_manager.task_dag} fallback={routing.product_manager.default} models={catalog.models} onChange={(choice) => updateChoice("product_manager", "task_dag", choice)} />
            <RoutingRow label="Repair planning" choice={routing.product_manager.repair} fallback={routing.product_manager.default} models={catalog.models} onChange={(choice) => updateChoice("product_manager", "repair", choice)} />
            <RoutingRow label="Update review" choice={routing.product_manager.update} fallback={routing.product_manager.default} models={catalog.models} onChange={(choice) => updateChoice("product_manager", "update", choice)} />
          </section>

          <section className="detail-panel stack">
            <div>
              <h2>Builder routing</h2>
              <p className="muted">Build tasks route by the validated <code>difficulty</code> field already present in each task DAG node. ProductManager does not choose model IDs.</p>
            </div>
            <RoutingRow label="Default" choice={routing.builder.default} models={catalog.models} onChange={(choice) => updateChoice("builder", "default", choice)} />
            <RoutingRow label="Easy task" choice={routing.builder.easy} fallback={routing.builder.default} models={catalog.models} onChange={(choice) => updateChoice("builder", "easy", choice)} />
            <RoutingRow label="Medium task" choice={routing.builder.medium} fallback={routing.builder.default} models={catalog.models} onChange={(choice) => updateChoice("builder", "medium", choice)} />
            <RoutingRow label="Hard task" choice={routing.builder.hard} fallback={routing.builder.default} models={catalog.models} onChange={(choice) => updateChoice("builder", "hard", choice)} />
            <RoutingRow label="Repair" choice={routing.builder.repair} fallback={routing.builder.default} models={catalog.models} onChange={(choice) => updateChoice("builder", "repair", choice)} />
            <RoutingRow label="Update" choice={routing.builder.update} fallback={routing.builder.default} models={catalog.models} onChange={(choice) => updateChoice("builder", "update", choice)} />
          </section>

          <section className="detail-panel stack">
            <div>
              <h2>Tester routing</h2>
              <p className="muted">Task tests, final end-to-end tests, and update tests may use separate settings.</p>
            </div>
            <RoutingRow label="Default" choice={routing.tester.default} models={catalog.models} onChange={(choice) => updateChoice("tester", "default", choice)} />
            <RoutingRow label="Task tests" choice={routing.tester.task} fallback={routing.tester.default} models={catalog.models} onChange={(choice) => updateChoice("tester", "task", choice)} />
            <RoutingRow label="Final end-to-end" choice={routing.tester.final_e2e} fallback={routing.tester.default} models={catalog.models} onChange={(choice) => updateChoice("tester", "final_e2e", choice)} />
            <RoutingRow label="Update tests" choice={routing.tester.update} fallback={routing.tester.default} models={catalog.models} onChange={(choice) => updateChoice("tester", "update", choice)} />
          </section>
          <div className="button-row">
            <button type="button" onClick={() => void saveRouting()} disabled={loading}>Save model routing</button>
          </div>
        </>
      )}
      {cliStatus && (
        <section className="detail-panel">
          <h2>Codex CLI</h2>
          <dl className="detail-grid">
            <div><dt>Status</dt><dd>{cliStatus.compatible ? "Compatible" : "Action required"}</dd></div>
            <div><dt>Version</dt><dd>{cliStatus.version ?? "unknown"}</dd></div>
            <div><dt>Source</dt><dd>{formatCliSource(cliStatus.source)}</dd></div>
            <div><dt>Minimum</dt><dd>{cliStatus.minimum_version ?? "not constrained"}</dd></div>
          </dl>
          <p className="muted"><strong>Executable:</strong> {cliStatus.resolved_path ?? "Not found"}</p>
          {cliStatus.error && <p className="error-text">{cliStatus.error}</p>}
        </section>
      )}
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

function RoutingRow({
  label,
  choice,
  fallback,
  models,
  onChange,
}: {
  label: string;
  choice: CodexInvocationChoice;
  fallback?: CodexInvocationChoice;
  models: CodexModelCatalog["models"];
  onChange: (choice: CodexInvocationChoice) => void;
}) {
  const defaultModel = models.find((model) => model.is_default) ?? models[0];
  const selectedModelName = choice.model ?? fallback?.model ?? defaultModel?.model ?? null;
  const selectedModel = models.find((model) => model.model === selectedModelName || model.id === selectedModelName);
  const efforts = selectedModel?.supported_reasoning_efforts ?? [];
  return (
    <div className="form-grid">
      <label>
        {label} model
        <select
          value={choice.model ?? ""}
          onChange={(event) => onChange({ model: event.target.value || null, reasoning_effort: null })}
        >
          <option value="">{fallback ? "Inherit role default" : "Codex default"}</option>
          {models.map((model) => (
            <option key={model.id} value={model.model}>{model.display_name} ({model.model})</option>
          ))}
        </select>
      </label>
      <label>
        {label} effort
        <select
          value={choice.reasoning_effort ?? ""}
          onChange={(event) => onChange({ ...choice, reasoning_effort: event.target.value || null })}
        >
          <option value="">{fallback ? "Inherit role/model default" : "Model default"}</option>
          {efforts.map((effort) => <option key={effort} value={effort}>{effort}</option>)}
        </select>
      </label>
    </div>
  );
}

function formatCliSource(source: string | null): string {
  if (source === "explicit_override") return "Explicit override";
  if (source === "codex_desktop") return "Codex Desktop";
  if (source === "path") return "PATH";
  return "unknown";
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
