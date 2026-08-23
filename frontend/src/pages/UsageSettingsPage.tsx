import { useEffect, useState } from "react";
import { NavLink } from "react-router-dom";

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
  NotionConnectionStatus,
  PermissionPolicy,
  api,
} from "../api/client";

export type SettingsSection = "usage" | "project" | "models" | "integrations" | "permissions";

const settingsSections: Array<{ section: SettingsSection; label: string; to: string }> = [
  { section: "usage", label: "Usage & CLI", to: "/settings/usage" },
  { section: "project", label: "Project builds", to: "/settings/project" },
  { section: "models", label: "Model routing", to: "/settings/models" },
  { section: "integrations", label: "Integrations", to: "/settings/integrations" },
  { section: "permissions", label: "Permissions", to: "/settings/permissions" },
];

const settingsPageCopy: Record<SettingsSection, { title: string; description: string }> = {
  usage: {
    title: "Usage & CLI",
    description: "Review the local Codex CLI and current account allowance windows.",
  },
  project: {
    title: "Project builds",
    description: "Choose how new Project builds select their workflow.",
  },
  models: {
    title: "Model routing",
    description: "Set the model and reasoning effort used by each bounded Codex action.",
  },
  integrations: {
    title: "Integrations",
    description: "Manage trusted connections and local companion services.",
  },
  permissions: {
    title: "Permissions",
    description: "Inspect the backend-owned policy applied to generated skills.",
  },
};

export default function UsageSettingsPage({ section = "usage" }: { section?: SettingsSection }) {
  const [usage, setUsage] = useState<CodexAccountUsage | null>(null);
  const [cliStatus, setCliStatus] = useState<CodexCliStatus | null>(null);
  const [catalog, setCatalog] = useState<CodexModelCatalog | null>(null);
  const [routing, setRouting] = useState<CodexRoutingSettings | null>(null);
  const [permissionPolicy, setPermissionPolicy] = useState<PermissionPolicy | null>(null);
  const [github, setGitHub] = useState<GitHubConnectionStatus | null>(null);
  const [githubToken, setGitHubToken] = useState("");
  const [notion, setNotion] = useState<NotionConnectionStatus | null>(null);
  const [notionToken, setNotionToken] = useState("");
  const [notionDataSourceId, setNotionDataSourceId] = useState("");
  const [atlas, setAtlas] = useState<AtlasIntegrationStatus | null>(null);
  const [atlasDirectory, setAtlasDirectory] = useState("");
  const [atlasPassphrase, setAtlasPassphrase] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [routingDirty, setRoutingDirty] = useState(false);
  const [loading, setLoading] = useState(true);

  async function loadSettings(refresh = false) {
    setError(null);
    setSaved(null);
    setLoading(true);
    try {
      if (section === "usage") {
        const [nextUsage, nextCliStatus] = await Promise.all([
          api.getCodexUsage(),
          api.getCodexCliStatus(refresh),
        ]);
        setUsage(nextUsage);
        setCliStatus(nextCliStatus);
      } else if (section === "project") {
        setRouting(await api.getCodexRoutingSettings());
        setRoutingDirty(false);
      } else if (section === "models") {
        const [nextCatalog, nextRouting] = await Promise.all([
          api.getCodexModels(refresh),
          api.getCodexRoutingSettings(),
        ]);
        setCatalog(nextCatalog);
        setRouting(nextRouting);
        setRoutingDirty(false);
      } else if (section === "integrations") {
        const [nextGitHub, nextAtlas, nextNotion] = await Promise.all([
          api.getGitHubConnection(),
          api.getAtlasStatus().catch((err) => atlasUnavailableStatus(err)),
          api.getNotionConnection().catch((err) => notionUnavailableStatus(err)),
        ]);
        setGitHub(nextGitHub);
        setAtlas(nextAtlas);
        setAtlasDirectory(atlasDirectoryForStatus(nextAtlas));
        setNotion(nextNotion);
        setNotionDataSourceId(nextNotion.data_source_id ?? "");
      } else {
        setPermissionPolicy(await api.getPermissionPolicy());
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : `Could not load ${settingsPageCopy[section].title.toLowerCase()}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadSettings();
  }, [section]);

  function updateChoice(
    group: "chat" | "product_manager" | "builder" | "tester",
    key: string,
    choice: CodexInvocationChoice,
  ) {
    setSaved(null);
    setRoutingDirty(true);
    setRouting((current) => {
      if (!current) return current;
      if (group === "chat") return { ...current, chat: choice };
      const nextGroup = { ...current[group], [key]: choice };
      return { ...current, [group]: nextGroup };
    });
  }

  async function saveRouting(successMessage: string) {
    if (!routing) return;
    setError(null);
    setSaved(null);
    setLoading(true);
    try {
      const { updated_at: _updatedAt, ...payload } = routing;
      const next = await api.updateCodexRoutingSettings(payload as CodexRoutingSettingsPayload);
      setRouting(next);
      setRoutingDirty(false);
      setSaved(successMessage);
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

  async function saveNotionConnection() {
    if (!notionToken || !notionDataSourceId.trim()) return;
    setError(null);
    setSaved(null);
    setLoading(true);
    try {
      const next = await api.putNotionConnection(notionToken, notionDataSourceId.trim());
      setNotion(next);
      setNotionToken("");
      setNotionDataSourceId(next.data_source_id ?? "");
      setSaved("Notion connection and todo data source validated and saved.");
    } catch (err) {
      setNotionToken("");
      setError(err instanceof Error ? err.message : "Could not save Notion connection");
    } finally {
      setLoading(false);
    }
  }

  async function removeNotionConnection() {
    setError(null);
    setSaved(null);
    setLoading(true);
    try {
      await api.removeNotionConnection();
      const next = await api.getNotionConnection();
      setNotion(next);
      setNotionToken("");
      setNotionDataSourceId("");
      setSaved("Notion connection removed.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not remove Notion connection");
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
          <h1>{settingsPageCopy[section].title}</h1>
          <p className="muted">{settingsPageCopy[section].description}</p>
        </div>
        <button type="button" className="secondary" onClick={() => void loadSettings(true)} disabled={loading}>
          Refresh
        </button>
      </header>
      <nav className="settings-nav" aria-label="Settings sections">
        {settingsSections.map((item) => (
          <NavLink
            key={item.section}
            to={item.to}
            className={({ isActive }) => (isActive ? "settings-nav-link active" : "settings-nav-link")}
          >
            {item.label}
          </NavLink>
        ))}
      </nav>
      {error && <p className="error-text">{error}</p>}
      {saved && <p className="success-text">{saved}</p>}
      {loading && <p className="muted" role="status">Loading {settingsPageCopy[section].title.toLowerCase()}…</p>}
      {section === "permissions" && permissionPolicy && (
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
      {section === "integrations" && github && (
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
      {section === "integrations" && notion && (
        <section className="detail-panel stack">
          <div>
            <h2>Notion todo connection</h2>
            <p className="muted">
              Eidolon validates one manually created Todo data source. The private connection token is stored only in Windows Credential Manager and is never shown to skills.
            </p>
            <p className="muted">
              Todo management stays in Notion, including the Notion iOS app. Eidolon does not keep a todo copy, cache, sync process, or todo page.
            </p>
          </div>
          <dl className="detail-grid">
            <div><dt>Status</dt><dd>{notion.connected ? "Connected" : notion.status}</dd></div>
            <div><dt>Workspace</dt><dd>{notion.workspace_name ?? "None"}</dd></div>
            <div><dt>Connection bot</dt><dd>{notion.bot_name ?? "None"}</dd></div>
            <div><dt>Data-source ID</dt><dd><code>{notion.data_source_id ?? "None"}</code></dd></div>
            <div><dt>Last validated</dt><dd>{formatDate(notion.last_validated_at)}</dd></div>
          </dl>
          {notion.error_type && <p className="error-text">Connection status: {notion.error_type.replace(/_/g, " ")}</p>}
          <label>
            Notion data-source ID
            <input
              type="text"
              value={notionDataSourceId}
              onChange={(event) => setNotionDataSourceId(event.target.value)}
              placeholder="Copy from Manage data sources in Notion"
            />
          </label>
          <label>
            {notion.connected ? "Replacement Notion token" : "Notion token"}
            <input
              type="password"
              autoComplete="new-password"
              value={notionToken}
              onChange={(event) => setNotionToken(event.target.value)}
              placeholder="Token is never displayed after submission"
            />
          </label>
          <div className="button-row">
            <button
              type="button"
              onClick={() => void saveNotionConnection()}
              disabled={loading || !notionToken || !notionDataSourceId.trim()}
            >
              {notion.connected ? "Replace Notion connection" : "Add Notion connection"}
            </button>
            {notion.connected && (
              <button type="button" className="secondary" onClick={() => void removeNotionConnection()} disabled={loading}>
                Remove Notion connection
              </button>
            )}
          </div>
        </section>
      )}
      {section === "integrations" && atlas && (
        <section className="detail-panel stack">
          <div>
            <h2>Eidolon-Atlas</h2>
            <p className="muted">
              Atlas is a local encrypted personal-data service. Functions use its native unlocked loopback API and require no API key. An optional passphrase can unlock an Atlas process started by Eidolon.
            </p>
            <p className="muted">
              Storing the passphrase shifts practical at-rest protection to your Windows account. There is no plaintext, environment-variable, configuration-file, SQLite-secret, or application-encrypted fallback.
            </p>
          </div>
          <dl className="detail-grid">
            <div><dt>Selected directory</dt><dd><code>{atlasDirectoryForStatus(atlas) || "Not configured"}</code></dd></div>
            <div><dt>Process</dt><dd>{atlasProcessLabel(atlas)}</dd></div>
            <div><dt>Atlas state</dt><dd>{atlasStateLabel(atlas)}</dd></div>
            <div><dt>Saved passphrase</dt><dd>{atlas.passphrase_configured ? "Configured" : "Not configured"}</dd></div>
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

          {!atlasOwned(atlas) && atlasRunning(atlas) && (
            <p className="muted">
              This Atlas process was launched externally. Unlock it through Atlas itself; Eidolon will never send a passphrase to it.
            </p>
          )}
          <label>
            {atlas.passphrase_configured ? "Replacement Atlas passphrase" : "Atlas passphrase"}
            <input
              type="password"
              autoComplete="new-password"
              value={atlasPassphrase}
              onChange={(event) => setAtlasPassphrase(event.target.value)}
              placeholder="Passphrase is never displayed after submission"
            />
          </label>
          <div className="button-row">
            <button type="button" onClick={() => void saveAtlasPassphrase()} disabled={loading || !atlasPassphrase || !atlasOwned(atlas)}>
              {atlas.passphrase_configured ? "Replace passphrase" : "Store passphrase"}
            </button>
            {atlas.passphrase_configured && (
              <button type="button" className="secondary" onClick={() => void removeAtlasPassphrase()} disabled={loading}>
                Remove passphrase
              </button>
            )}
            <button type="button" className="secondary" onClick={() => void unlockAtlas()} disabled={loading || !atlas.passphrase_configured || !atlasOwned(atlas)}>
              Unlock now
            </button>
          </div>
        </section>
      )}
      {section === "models" && catalog && !catalog.available && (
        <p className="error-text">Model choices are unavailable: {catalog.error ?? "Codex model catalog could not be loaded."}</p>
      )}
      {section === "project" && routing && (
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
                setRoutingDirty(true);
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
            <button type="button" onClick={() => void saveRouting("Project workflow saved.")} disabled={loading}>Save project workflow</button>
          </div>
          <RoutingSaveStatus routing={routing} dirty={routingDirty} />
        </section>
      )}
      {section === "models" && routing && catalog?.available && (
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
            <div className="button-row">
              <button type="button" onClick={() => void saveRouting("Model routing saved.")} disabled={loading}>Save model routing</button>
            </div>
            <RoutingSaveStatus routing={routing} dirty={routingDirty} />
          </section>
        </>
      )}
      {section === "usage" && cliStatus && (
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
      {section === "usage" && usage && !usage.available && <p className="error-text">{usage.error ?? "Codex usage is unavailable."}</p>}
      {section === "usage" && usage?.available && (
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
      {section === "usage" && (
        <p className="muted">
          DAG builds pause before the next ready batch when either allowance window has less than 5% remaining. Skill runtime calls are intentionally excluded from build token accounting.
        </p>
      )}
    </section>
  );
}

function atlasUnavailableStatus(err: unknown): AtlasIntegrationStatus {
  return {
    provider: "atlas",
    directory: "",
    process_ownership: "none",
    running: false,
    initialized: null,
    locked: null,
    passphrase_configured: false,
    startup_error: err instanceof Error ? err.message : "Atlas status is unavailable",
  };
}

function notionUnavailableStatus(err: unknown): NotionConnectionStatus {
  return {
    provider: "notion",
    connected: false,
    status: "unavailable",
    bot_name: null,
    bot_id: null,
    workspace_name: null,
    data_source_id: null,
    last_validated_at: null,
    created_at: null,
    updated_at: null,
    error_type: err instanceof Error ? err.message : "unavailable",
  };
}

function atlasDirectoryForStatus(status: AtlasIntegrationStatus): string {
  return status.directory;
}

function atlasOwned(status: AtlasIntegrationStatus): boolean {
  return status.process_ownership === "owned";
}

function atlasRunning(status: AtlasIntegrationStatus): boolean {
  return status.running;
}

function atlasError(status: AtlasIntegrationStatus): string | null {
  return status.startup_error ?? status.error_type ?? null;
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

function RoutingSaveStatus({ routing, dirty }: { routing: CodexRoutingSettings; dirty: boolean }) {
  if (dirty) return <p className="muted" aria-live="polite">Unsaved Codex routing changes.</p>;
  if (routing.updated_at) {
    return <p className="muted" aria-live="polite">Saved {formatDate(routing.updated_at)}. Future invocations use these routes.</p>;
  }
  return <p className="muted" aria-live="polite">Using Codex defaults. No routing override has been saved yet.</p>;
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
