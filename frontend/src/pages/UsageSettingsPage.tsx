import { useEffect, useState } from "react";

import {
  AtlasIntegrationStatus,
  CodexAccountUsage,
  CodexCliStatus,
  CodexInvocationChoice,
  CodexModelCatalog,
  CodexRoutingSettings,
  CodexRoutingSettingsPayload,
  CodexUsageWindow,
  GitHubConnectionStatus,
  PermissionPolicy,
  api,
} from "../api/client";

export default function UsageSettingsPage() {
  const [usage, setUsage] = useState<CodexAccountUsage | null>(null);
  const [cliStatus, setCliStatus] = useState<CodexCliStatus | null>(null);
  const [catalog, setCatalog] = useState<CodexModelCatalog | null>(null);
  const [routing, setRouting] = useState<CodexRoutingSettings | null>(null);
  const [permissionPolicy, setPermissionPolicy] = useState<PermissionPolicy | null>(null);
  const [github, setGitHub] = useState<GitHubConnectionStatus | null>(null);
  const [githubToken, setGitHubToken] = useState("");
  const [atlas, setAtlas] = useState<AtlasIntegrationStatus | null>(null);
  const [atlasDirectory, setAtlasDirectory] = useState("");
  const [atlasApiKey, setAtlasApiKey] = useState("");
  const [atlasPassphrase, setAtlasPassphrase] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  async function loadUsage(refresh = false) {
    setError(null);
    try {
      const [coreSettings, nextAtlas] = await Promise.all([
        Promise.all([
          api.getCodexUsage(),
          api.getCodexCliStatus(refresh),
          api.getCodexModels(refresh),
          api.getCodexRoutingSettings(),
          api.getGitHubConnection(),
          api.getPermissionPolicy(),
        ]),
        api.getAtlasStatus().catch((err) => atlasUnavailableStatus(err)),
      ]);
      const [nextUsage, nextCliStatus, nextCatalog, nextRouting, nextGitHub, nextPermissionPolicy] = coreSettings;
      setUsage(nextUsage);
      setCliStatus(nextCliStatus);
      setCatalog(nextCatalog);
      setRouting(nextRouting);
      setGitHub(nextGitHub);
      setPermissionPolicy(nextPermissionPolicy);
      setAtlas(nextAtlas);
      setAtlasDirectory(atlasDirectoryForStatus(nextAtlas));
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
      setSaved("Codex settings saved.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save Codex settings");
    } finally {
      setLoading(false);
    }
  }

  async function saveGitHubConnection() {
    if (!githubToken.trim()) return;
    setError(null);
    setSaved(null);
    setLoading(true);
    try {
      const next = await api.putGitHubConnection(githubToken);
      setGitHub(next);
      setGitHubToken("");
      setSaved("GitHub connection validated and saved.");
    } catch (err) {
      setGitHubToken("");
      setError(err instanceof Error ? err.message : "Could not save GitHub connection");
    } finally {
      setLoading(false);
    }
  }

  async function removeGitHubConnection() {
    setError(null);
    setSaved(null);
    setLoading(true);
    try {
      await api.removeGitHubConnection();
      setGitHub(await api.getGitHubConnection());
      setGitHubToken("");
      setSaved("GitHub connection removed.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not remove GitHub connection");
    } finally {
      setLoading(false);
    }
  }

  async function saveAtlasDirectory() {
    if (!atlasDirectory.trim()) return;
    setError(null);
    setSaved(null);
    setLoading(true);
    try {
      const next = await api.updateAtlasDirectory(atlasDirectory.trim());
      setAtlas(next);
      setAtlasDirectory(atlasDirectoryForStatus(next));
      setSaved("Atlas directory saved and Atlas restarted.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the Atlas directory");
    } finally {
      setLoading(false);
    }
  }

  async function restartAtlas() {
    setError(null);
    setSaved(null);
    setLoading(true);
    try {
      const next = await api.restartAtlas();
      setAtlas(next);
      setAtlasDirectory(atlasDirectoryForStatus(next));
      setSaved("Atlas restarted.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not restart Atlas");
    } finally {
      setLoading(false);
    }
  }

  async function saveAtlasApiKey() {
    if (!atlasApiKey.trim()) return;
    setError(null);
    setSaved(null);
    setLoading(true);
    try {
      const next = await api.putAtlasApiKey(atlasApiKey);
      setAtlas(next);
      setAtlasApiKey("");
      setSaved("Atlas API key validated and saved.");
    } catch (err) {
      setAtlasApiKey("");
      setError(err instanceof Error ? err.message : "Could not save the Atlas API key");
    } finally {
      setLoading(false);
    }
  }

  async function removeAtlasApiKey() {
    setError(null);
    setSaved(null);
    setLoading(true);
    try {
      await api.removeAtlasApiKey();
      const next = await api.getAtlasStatus();
      setAtlas(next);
      setAtlasApiKey("");
      setSaved("Atlas API key removed.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not remove the Atlas API key");
    } finally {
      setLoading(false);
    }
  }

  async function saveAtlasPassphrase() {
    if (!atlasPassphrase) return;
    setError(null);
    setSaved(null);
    setLoading(true);
    try {
      const next = await api.putAtlasPassphrase(atlasPassphrase);
      setAtlas(next);
      setAtlasPassphrase("");
      setSaved("Atlas passphrase verified and automatic unlock is configured.");
    } catch (err) {
      setAtlasPassphrase("");
      setError(err instanceof Error ? err.message : "Could not save the Atlas passphrase");
    } finally {
      setLoading(false);
    }
  }

  async function removeAtlasPassphrase() {
    setError(null);
    setSaved(null);
    setLoading(true);
    try {
      await api.removeAtlasPassphrase();
      const next = await api.getAtlasStatus();
      setAtlas(next);
      setAtlasPassphrase("");
      setSaved("Atlas automatic unlock passphrase removed.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not remove the Atlas passphrase");
    } finally {
      setLoading(false);
    }
  }

  async function unlockAtlas() {
    setError(null);
    setSaved(null);
    setLoading(true);
    try {
      const next = await api.unlockAtlas();
      setAtlas(next);
      setSaved("Atlas unlock requested.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not unlock Atlas");
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
      {permissionPolicy && (
        <section className="detail-panel stack">
          <div>
            <h2>Permission policy</h2>
            <p className="muted">
              Read-only current policy loaded from <code>{permissionPolicy.source}</code>. Agent instructions do not duplicate these classifications.
            </p>
          </div>
          <div className="section-grid">
            <div>
              <h3>Default allowed</h3>
              <pre>{JSON.stringify(permissionPolicy.default_allowed, null, 2)}</pre>
            </div>
            <div>
              <h3>Requires approval</h3>
              <pre>{JSON.stringify(permissionPolicy.requires_approval, null, 2)}</pre>
            </div>
          </div>
          <div>
            <h3>Blocked</h3>
            <ul>
              {permissionPolicy.blocked.map((capability) => <li key={capability}>{capability}</li>)}
            </ul>
          </div>
          <div>
            <h3>Web application policy</h3>
            <pre>{JSON.stringify(permissionPolicy.web_app, null, 2)}</pre>
          </div>
        </section>
      )}
      {github && (
        <section className="detail-panel stack">
          <div>
            <h2>GitHub connection</h2>
            <p className="muted">
              The token is validated by trusted backend code and stored in Windows Credential Manager. It is never shown again or shared with skills.
            </p>
          </div>
          <dl className="detail-grid">
            <div><dt>Status</dt><dd>{github.connected ? "Connected" : github.status}</dd></div>
            <div><dt>Account</dt><dd>{github.account_login ?? "None"}</dd></div>
            <div><dt>Account ID</dt><dd>{github.account_id ?? "None"}</dd></div>
            <div><dt>Last validated</dt><dd>{formatDate(github.last_validated_at)}</dd></div>
          </dl>
          {github.error_type && <p className="error-text">Connection status: {github.error_type.replace(/_/g, " ")}</p>}
          <label>
            {github.connected ? "Replacement GitHub token" : "GitHub token"}
            <input
              type="password"
              autoComplete="new-password"
              value={githubToken}
              onChange={(event) => setGitHubToken(event.target.value)}
              placeholder="Token is never displayed after submission"
            />
          </label>
          <div className="button-row">
            <button type="button" onClick={() => void saveGitHubConnection()} disabled={loading || !githubToken.trim()}>
              {github.connected ? "Replace connection" : "Add connection"}
            </button>
            {github.connected && (
              <button type="button" className="secondary" onClick={() => void removeGitHubConnection()} disabled={loading}>
                Remove connection
              </button>
            )}
          </div>
        </section>
      )}
      {atlas && (
        <section className="detail-panel stack">
          <div>
            <h2>Eidolon-Atlas</h2>
            <p className="muted">
              Atlas is a local encrypted personal-data service. Eidolon starts the selected instance and keeps its API key and optional passphrase in Windows Credential Manager; neither secret is shown again or shared with skills.
            </p>
            <p className="muted">
              Storing the passphrase shifts practical at-rest protection to your Windows account. There is no plaintext, environment-variable, configuration-file, SQLite-secret, or application-encrypted fallback.
            </p>
          </div>
          <dl className="detail-grid">
            <div><dt>Selected directory</dt><dd><code>{atlasDirectoryForStatus(atlas) || "Not configured"}</code></dd></div>
            <div><dt>Process</dt><dd>{atlasProcessLabel(atlas)}</dd></div>
            <div><dt>Atlas state</dt><dd>{atlasStateLabel(atlas)}</dd></div>
            <div><dt>API key</dt><dd>{atlasKeyLabel(atlas)}</dd></div>
            <div><dt>Automatic unlock</dt><dd>{atlasAutoUnlock(atlas) ? "Configured" : "Not configured"}</dd></div>
          </dl>
          {atlasError(atlas) && <p className="error-text">Atlas status: {atlasError(atlas)}</p>}

          <label>
            Atlas directory (absolute path)
            <input
              type="text"
              value={atlasDirectory}
              onChange={(event) => setAtlasDirectory(event.target.value)}
              placeholder="C:\\Users\\you\\Projects\\Eidolon-Atlas"
            />
          </label>
          <div className="button-row">
            <button type="button" onClick={() => void saveAtlasDirectory()} disabled={loading || !atlasDirectory.trim()}>
              Save and restart Atlas
            </button>
            <button type="button" className="secondary" onClick={() => void restartAtlas()} disabled={loading}>
              Restart Atlas
            </button>
          </div>

          <label>
            {atlasKeyConnected(atlas) ? "Replacement Atlas API key" : "Atlas API key"}
            <input
              type="password"
              autoComplete="new-password"
              value={atlasApiKey}
              onChange={(event) => setAtlasApiKey(event.target.value)}
              placeholder="Key is never displayed after submission"
            />
          </label>
          <div className="button-row">
            <button type="button" onClick={() => void saveAtlasApiKey()} disabled={loading || !atlasApiKey.trim()}>
              {atlasKeyConnected(atlas) ? "Replace API key" : "Add API key"}
            </button>
            {atlasKeyConnected(atlas) && (
              <button type="button" className="secondary" onClick={() => void removeAtlasApiKey()} disabled={loading}>
                Remove API key
              </button>
            )}
          </div>

          <label>
            {atlasAutoUnlock(atlas) ? "Replacement Atlas passphrase" : "Atlas passphrase"}
            <input
              type="password"
              autoComplete="new-password"
              value={atlasPassphrase}
              onChange={(event) => setAtlasPassphrase(event.target.value)}
              placeholder="Passphrase is never displayed after submission"
            />
          </label>
          <div className="button-row">
            <button type="button" onClick={() => void saveAtlasPassphrase()} disabled={loading || !atlasPassphrase || !atlasKeyConnected(atlas)}>
              {atlasAutoUnlock(atlas) ? "Replace passphrase" : "Store passphrase"}
            </button>
            {atlasAutoUnlock(atlas) && (
              <button type="button" className="secondary" onClick={() => void removeAtlasPassphrase()} disabled={loading}>
                Remove passphrase
              </button>
            )}
            <button type="button" className="secondary" onClick={() => void unlockAtlas()} disabled={loading || !atlasAutoUnlock(atlas)}>
              Unlock now
            </button>
          </div>
        </section>
      )}
      {catalog && !catalog.available && (
        <p className="error-text">Model choices are unavailable: {catalog.error ?? "Codex model catalog could not be loaded."}</p>
      )}
      {routing && (
        <section className="detail-panel stack">
          <div>
            <h2>Project build workflow</h2>
            <p className="muted">Override ProductManager workflow selection for every new Project build.</p>
          </div>
          <label>
            Workflow selection
            <select
              value={routing.project_build_workflow_override ?? ""}
              onChange={(event) => {
                setSaved(null);
                setRouting({
                  ...routing,
                  project_build_workflow_override:
                    (event.target.value || null) as CodexRoutingSettingsPayload["project_build_workflow_override"],
                });
              }}
            >
              <option value="">Automatic (ProductManager chooses)</option>
              <option value="single_codex">Simple (single Codex)</option>
              <option value="task_dag">Task DAG</option>
            </select>
          </label>
          <p className="muted">
            Simple and Task DAG are hard overrides. Automatic preserves ProductManager selection.
          </p>
          <div className="button-row">
            <button type="button" onClick={() => void saveRouting()} disabled={loading}>Save Codex settings</button>
          </div>
        </section>
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
            <RoutingRow label="Project planning and clarification" choice={routing.product_manager.blueprint_and_permissions} fallback={routing.product_manager.default} models={catalog.models} onChange={(choice) => updateChoice("product_manager", "blueprint_and_permissions", choice)} />
            <RoutingRow label="Task DAG" choice={routing.product_manager.task_dag} fallback={routing.product_manager.default} models={catalog.models} onChange={(choice) => updateChoice("product_manager", "task_dag", choice)} />
            <RoutingRow label="Repair planning" choice={routing.product_manager.repair} fallback={routing.product_manager.default} models={catalog.models} onChange={(choice) => updateChoice("product_manager", "repair", choice)} />
            <RoutingRow label="Update review" choice={routing.product_manager.update} fallback={routing.product_manager.default} models={catalog.models} onChange={(choice) => updateChoice("product_manager", "update", choice)} />
          </section>

          <section className="detail-panel stack">
            <div>
              <h2>Builder routing</h2>
              <p className="muted">Single Codex has its own route. Task DAG builds route by each backend-validated node difficulty. ProductManager does not choose model IDs.</p>
            </div>
            <RoutingRow label="Default" choice={routing.builder.default} models={catalog.models} onChange={(choice) => updateChoice("builder", "default", choice)} />
            <RoutingRow label="Single Codex" choice={routing.builder.single_codex} fallback={routing.builder.default} models={catalog.models} onChange={(choice) => updateChoice("builder", "single_codex", choice)} />
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

function atlasUnavailableStatus(err: unknown): AtlasIntegrationStatus {
  return {
    provider: "atlas",
    directory: null,
    process_owned: false,
    process_running: false,
    process_ownership: "none",
    running: false,
    initialized: null,
    locked: null,
    key_connected: false,
    key_status: "unavailable",
    api_key_status: "unavailable",
    auto_unlock_configured: false,
    startup_error: err instanceof Error ? err.message : "Atlas status is unavailable",
  };
}

function atlasDirectoryForStatus(status: AtlasIntegrationStatus): string {
  return status.directory ?? status.selected_directory ?? status.resolved_directory ?? "";
}

function atlasOwned(status: AtlasIntegrationStatus): boolean {
  if (status.process_owned !== undefined) return status.process_owned;
  if (status.owned !== undefined) return status.owned;
  return status.process_ownership === "owned";
}

function atlasRunning(status: AtlasIntegrationStatus): boolean {
  return status.process_running ?? status.running ?? false;
}

function atlasKeyConnected(status: AtlasIntegrationStatus): boolean {
  return status.key_connected === true
    || status.api_key_connected === true
    || status.key_status === "connected"
    || status.api_key_status === "connected";
}

function atlasAutoUnlock(status: AtlasIntegrationStatus): boolean {
  return status.auto_unlock_configured ?? status.passphrase_configured ?? false;
}

function atlasError(status: AtlasIntegrationStatus): string | null {
  return status.error ?? status.error_message ?? status.startup_error ?? status.error_type ?? null;
}

function atlasProcessLabel(status: AtlasIntegrationStatus): string {
  if (!atlasRunning(status)) return "Not running";
  return atlasOwned(status) ? "Running (Eidolon-owned)" : "Running (external process)";
}

function atlasStateLabel(status: AtlasIntegrationStatus): string {
  if (status.initialized === null || status.locked === null) return "Unavailable";
  if (!status.initialized) return "Not initialized";
  return status.locked ? "Locked" : "Unlocked";
}

function atlasKeyLabel(status: AtlasIntegrationStatus): string {
  if (atlasKeyConnected(status)) return "Connected";
  const keyStatus = status.key_status ?? status.api_key_status ?? "disconnected";
  return keyStatus.replace(/_/g, " ");
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
