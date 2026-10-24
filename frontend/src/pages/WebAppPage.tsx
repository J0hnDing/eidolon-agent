import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import {
  Skill,
  WebAppAuditRecord,
  WebAppInstance,
  WebAppOpenResponse,
  api,
} from "../api/client";
import { AppIcon } from "../components/AppIcon";
import { Modal } from "../components/Modal";
import { formatDisplayName } from "../lib/displayName";
import { formatSystemDateTime } from "../lib/dateTime";
import { usePolling } from "../lib/usePolling";
import "../features/skill-detail/skill-detail.css";
import "./web-app-details.css";

export const WEB_APP_IFRAME_SANDBOX = "allow-scripts allow-forms allow-same-origin allow-modals";
const WEB_APP_GATEWAY_DOMAIN = (import.meta.env.VITE_WEB_APP_GATEWAY_DOMAIN ?? "web-app.localhost").toLowerCase();

export function isControlledWebAppEmbedUrl(value: string) {
  try {
    const url = new URL(value);
    return (
      (url.protocol === "http:" || url.protocol === "https:") &&
      url.hostname.toLowerCase().endsWith(`.${WEB_APP_GATEWAY_DOMAIN}`) &&
      url.username === "" &&
      url.password === ""
    );
  } catch {
    return false;
  }
}

export function WebAppFrame({ app, title }: { app: WebAppOpenResponse; title: string }) {
  if (!isControlledWebAppEmbedUrl(app.embed_url)) {
    return <p className="error-text" role="alert">The backend returned an invalid application origin.</p>;
  }
  return (
    <iframe
      className="web-app-frame"
      src={app.embed_url}
      title={title}
      sandbox={WEB_APP_IFRAME_SANDBOX}
      allow=""
      referrerPolicy="no-referrer"
    />
  );
}

export default function WebAppPage() {
  const { skillId } = useParams();
  const id = Number(skillId);
  const [skill, setSkill] = useState<Skill | null>(null);
  const [openedApp, setOpenedApp] = useState<WebAppOpenResponse | null>(null);
  const [instances, setInstances] = useState<WebAppInstance[]>([]);
  const [audit, setAudit] = useState<WebAppAuditRecord[]>([]);
  const [isWorking, setIsWorking] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const openingSkillId = useRef<number | null>(null);

  useEffect(() => {
    if (!Number.isFinite(id)) return;
    openApplication();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  usePolling(() => refreshDiagnostics(), Boolean(openedApp?.instance.status === "healthy"), 5000);

  async function openApplication() {
    if (openingSkillId.current === id) return;
    openingSkillId.current = id;
    setIsWorking(true);
    setError(null);
    try {
      const loadedSkill = await api.getSkill(id);
      setSkill(loadedSkill);
      if (loadedSkill.runtime !== "web_app") throw new Error("This skill does not use the web_app runtime");
      const opened = await api.openWebApp(id);
      setOpenedApp(opened);
      setInstances([opened.instance]);
      await refreshDiagnostics();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not open application");
    } finally {
      if (openingSkillId.current === id) openingSkillId.current = null;
      setIsWorking(false);
    }
  }

  async function refreshDiagnostics() {
    if (!Number.isFinite(id)) return;
    const [loadedInstances, loadedAudit] = await Promise.all([
      api.listWebAppInstances(id),
      api.listWebAppAudit(id),
    ]);
    setInstances(loadedInstances);
    setAudit(loadedAudit);
  }

  async function stopApplication() {
    setIsWorking(true);
    setError(null);
    try {
      setInstances(await api.stopWebApp(id));
      setOpenedApp(null);
      setAudit(await api.listWebAppAudit(id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not stop application");
    } finally {
      setIsWorking(false);
    }
  }

  const currentInstance = instances[0] ?? openedApp?.instance ?? null;

  return (
    <section className="web-app-page web-app-workspace stack">
      <nav className="skill-breadcrumb" aria-label="Breadcrumb"><Link to="/apps"><AppIcon name="back" size={15} />Back to Applications</Link><AppIcon name="chevron" size={12} /><span>{skill ? formatDisplayName(skill.name) : "Application"}</span></nav>
      <header className="web-app-trusted-chrome">
        <div className="web-app-identity"><span className="skill-identity-icon"><AppIcon name="apps" size={23} /></span><div>
          <h1>{skill ? formatDisplayName(skill.name) : "Web application"}</h1>
          <p>{skill?.description}</p>
        </div></div>
        <div className="button-row">
          <span className={`badge status-${currentInstance?.status ?? "pending"}`}>
            {currentInstance?.status ?? (isWorking ? "starting" : "stopped")}
          </span>
          {openedApp ? (
            <button type="button" className="secondary" onClick={stopApplication} disabled={isWorking}><AppIcon name="pause" size={14} />Stop</button>
          ) : (
            <button type="button" onClick={openApplication} disabled={isWorking}><AppIcon name="play" size={14} />Start</button>
          )}
          <Link className="button-link secondary" to={`/skills/${id}`}>Skill details<AppIcon name="arrow" size={14} /></Link>
        </div>
      </header>

      {error && <p className="error-text" role="alert">{error}</p>}
      {isWorking && !openedApp && <div className="web-app-placeholder"><AppIcon name="apps" size={32} /><h2>Opening your application</h2><p>Starting the version-pinned application and waiting for readiness...</p></div>}
      {!isWorking && !openedApp && !error && <div className="web-app-placeholder"><AppIcon name="apps" size={32} /><h2>Application is stopped</h2><p>Use Start to open this application again.</p></div>}

      {openedApp && (
        <>
          <section className="web-app-containment-strip" aria-label="Application containment status">
            <AppIcon name="shield" size={14} />
            <span>Version #{openedApp.instance.version_id}</span>
            <span>{openedApp.instance.runner_mode === "docker" ? "Docker sandbox" : "Local dev fallback"}</span>
            <span>Isolated session origin</span>
            <span>Runtime permissions approved - {skill?.risk_level ?? "unknown"} risk</span>
            <span>Browser network: same origin only</span>
          </section>
          <WebAppFrame
            app={openedApp}
            title={`${skill ? formatDisplayName(skill.name) : "Skill"} application`}
          />
        </>
      )}

      <div className="web-app-detail-entry"><button type="button" className="secondary compact" onClick={() => setDiagnosticsOpen(true)}><AppIcon name="activity" size={16} />Containment and lifecycle details</button></div>
      {diagnosticsOpen && <Modal title="Application diagnostics" className="web-app-detail-modal" onClose={() => setDiagnosticsOpen(false)}>
        <div className="web-app-diagnostics-content stack">
        {openedApp && <section className="stack"><h2>Application containment</h2><dl className="detail-grid">
          <div><dt>Origin isolation</dt><dd>{openedApp.containment.origin_isolation}</dd></div>
          <div><dt>Runner</dt><dd>{openedApp.containment.runner_isolation}</dd></div>
          <div><dt>Browser network</dt><dd>{openedApp.containment.browser_network}</dd></div>
          <div><dt>Server network</dt><dd>{openedApp.containment.server_network_enforcement}</dd></div>
          <div><dt>WebSockets</dt><dd>{openedApp.containment.websocket_support}</dd></div>
          <div><dt>Session expires</dt><dd>{formatSystemDateTime(openedApp.session.expires_at)}</dd></div>
        </dl></section>}
        <h2>Lifecycle diagnostics</h2>
        {currentInstance?.error_message && <p className="error-text">{currentInstance.error_message}</p>}
        {currentInstance?.logs && <pre>{currentInstance.logs}</pre>}
        {audit.length > 0 ? (
          <div className="run-list">
            {audit.slice(0, 12).map((record) => (
              <article className="run-row" key={record.id}>
                <div>
                  <strong>{record.operation}</strong>
                  <span>{formatSystemDateTime(record.started_at)}</span>
                </div>
                <span className={`badge status-${record.status}`}>{record.status}</span>
              </article>
            ))}
          </div>
        ) : (
          <p className="muted">No lifecycle records yet.</p>
        )}
        </div>
      </Modal>}
    </section>
  );
}
