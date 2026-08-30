import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";

import {
  Skill,
  WebAppAuditRecord,
  WebAppInstance,
  WebAppOpenResponse,
  api,
} from "../api/client";
import { formatDisplayName } from "../lib/displayName";
import { formatSystemDateTime } from "../lib/dateTime";
import { usePolling } from "../lib/usePolling";

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
    <section className="web-app-page stack">
      <header className="web-app-trusted-chrome">
        <div>
          <p className="eyebrow">Trusted Eidolon application chrome</p>
          <h1>{skill ? formatDisplayName(skill.name) : "Web application"}</h1>
          <p>{skill?.description}</p>
        </div>
        <div className="button-row">
          <span className={`badge status-${currentInstance?.status ?? "pending"}`}>
            {currentInstance?.status ?? (isWorking ? "starting" : "stopped")}
          </span>
          {openedApp ? (
            <button type="button" className="secondary" onClick={stopApplication} disabled={isWorking}>Stop</button>
          ) : (
            <button type="button" onClick={openApplication} disabled={isWorking}>Start</button>
          )}
        </div>
      </header>

      {error && <p className="error-text">{error}</p>}
      {isWorking && !openedApp && <p className="muted">Starting the version-pinned application and waiting for readiness...</p>}

      {openedApp && (
        <>
          <section className="web-app-containment-strip" aria-label="Application containment status">
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
          <details className="detail-panel">
            <summary>Containment and lifecycle details</summary>
            <dl className="detail-grid">
              <div><dt>Origin isolation</dt><dd>{openedApp.containment.origin_isolation}</dd></div>
              <div><dt>Runner</dt><dd>{openedApp.containment.runner_isolation}</dd></div>
              <div><dt>Browser network</dt><dd>{openedApp.containment.browser_network}</dd></div>
              <div><dt>Server network</dt><dd>{openedApp.containment.server_network_enforcement}</dd></div>
              <div><dt>WebSockets</dt><dd>{openedApp.containment.websocket_support}</dd></div>
              <div><dt>Session expires</dt><dd>{formatSystemDateTime(openedApp.session.expires_at)}</dd></div>
            </dl>
          </details>
        </>
      )}

      <section className="detail-panel">
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
      </section>
    </section>
  );
}
