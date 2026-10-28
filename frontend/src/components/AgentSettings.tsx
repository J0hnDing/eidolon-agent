import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { AgentId, CodexInvocationChoice, CodexModelCatalog, api } from "../api/client";
import { RoutingRow } from "./ModelRoutingRow";
import "./agent-configuration.css";

const names = { act: "Act", observer: "Observer", assistant: "Assistant" };
const routes = { act: "act", observer: "observer", assistant: "assessment" } as const;

export function AgentModelSettings({ agentId }: { agentId: AgentId }) {
  const [choice, setChoice] = useState<CodexInvocationChoice | null>(null);
  const [catalog, setCatalog] = useState<CodexModelCatalog | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    void Promise.all([api.getCodexRoutingSettings(), api.getCodexModels()]).then(([routing, models]) => {
      if (cancelled) return;
      setChoice(routing[routes[agentId]]);
      setCatalog(models);
      if (!models.available) setError(models.error ?? "Model choices are unavailable.");
    }).catch((reason) => {
      if (!cancelled) setError(reason instanceof Error ? reason.message : "Could not load model settings.");
    });
    return () => { cancelled = true; };
  }, [agentId, revision]);

  return <section className="detail-panel stack agent-model-settings" aria-label={`${names[agentId]} model settings`}>
    <div><h2>Model settings</h2><p className="muted">Shared with <Link to="/settings/models">Model routing</Link>. Saved choices apply to subsequent turns.</p></div>
    {error && <p className="error-text" role="alert">{error} {(!choice || !catalog?.available) && <button type="button" className="secondary compact" onClick={() => setRevision((value) => value + 1)}>Retry</button>}</p>}
    {!choice && !error && <p className="muted" role="status">Loading model settings…</p>}
    {choice && catalog?.available && <form className="stack" onSubmit={async (event) => {
      event.preventDefault(); setSaving(true); setError(null); setSaved(false);
      try {
        const routing = await api.updateAgentModel(agentId, choice);
        setChoice(routing[routes[agentId]]); setDirty(false); setSaved(true);
      } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not save model settings."); }
      finally { setSaving(false); }
    }}>
      <fieldset disabled={saving} className="agent-model-fields"><RoutingRow label={names[agentId]} choice={choice} models={catalog.models} onChange={(value) => { setChoice(value); setDirty(true); setSaved(false); }} /></fieldset>
      <div className="button-row"><button type="submit" disabled={saving || !dirty}>{saving ? "Saving…" : "Save model settings"}</button><span className="muted" role="status">{dirty ? "Unsaved model changes." : saved ? "Model settings saved." : ""}</span></div>
    </form>}
  </section>;
}

export function AgentInstructions({ agentId }: { agentId: AgentId }) {
  const [instructions, setInstructions] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setError(null);
    void api.getAgentInstructions(agentId).then((result) => { if (!cancelled) setInstructions(result.instructions); })
      .catch((reason) => { if (!cancelled) setError(reason instanceof Error ? reason.message : "Could not load agent instructions."); });
    return () => { cancelled = true; };
  }, [agentId, revision]);
  return <section className="detail-panel stack agent-instructions" aria-label={`${names[agentId]} instructions`}>
    <div><h2>Agent instructions</h2><p className="muted">Read-only base instructions used by the current {names[agentId]} runtime. Individual turns also receive task context and tool definitions.</p></div>
    {error && <p className="error-text" role="alert">{error} <button type="button" className="secondary compact" onClick={() => setRevision((value) => value + 1)}>Retry</button></p>}
    {instructions === null && !error && <p className="muted" role="status">Loading instructions…</p>}
    {instructions !== null && <pre tabIndex={0} aria-label={`${names[agentId]} base instructions`}>{instructions}</pre>}
  </section>;
}
