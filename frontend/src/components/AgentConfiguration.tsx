import { useEffect, useState } from "react";
import { AgentDefinition, AgentId, AgentPolicy, api } from "../api/client";
import { AgentFunctionList, AgentPolicyEditor } from "./AgentControls";
import { Modal } from "./Modal";

export function AgentConfiguration({ agentId }: { agentId: AgentId }) {
  const [agent, setAgent] = useState<AgentDefinition | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [policy, setPolicy] = useState<AgentPolicy | null>(null);
  const [saving, setSaving] = useState(false);
  const [revision, setRevision] = useState(0);
  const [view, setView] = useState<"policy" | "functions">("policy");
  useEffect(() => {
    let cancelled = false;
    setError(null);
    void api.getAgent(agentId).then((value) => { if (!cancelled) setAgent(value); })
      .catch((reason) => { if (!cancelled) setError(reason instanceof Error ? reason.message : "Could not load agent configuration"); });
    return () => { cancelled = true; };
  }, [agentId, revision]);

  return <section className="stack agent-configuration" aria-label="Agent configuration">
    {error && <div className="error-text" role="alert">{error} <button type="button" className="secondary compact" onClick={() => setRevision((value) => value + 1)}>Retry</button></div>}
    {!agent && !error && <p className="muted" role="status">Loading configuration…</p>}
    {agent && <>
      <div className="agent-boundary-summary" aria-label="Agent boundaries">
        <div><small>Filesystem access</small><strong>{agent.permissions.filesystem}</strong></div>
        <div><small>Web search</small><strong>{agent.permissions.web_search ? "Allowed" : "Blocked"}</strong></div>
      </div>
      <div className="page-tabs agent-configuration-nav" role="group" aria-label="Configuration sections">
        <button type="button" aria-pressed={view === "policy"} className={view === "policy" ? "active" : ""} onClick={() => setView("policy")}>Policy &amp; model</button>
        <button type="button" aria-pressed={view === "functions"} className={view === "functions" ? "active" : ""} onClick={() => setView("functions")}>Effective functions <span>{agent.functions.length}</span></button>
      </div>
      <div className="agent-configuration-sections">
        <div hidden={view !== "policy"}><AgentPolicyEditor key={JSON.stringify(agent.policy)} agent={agent} isWorking={saving} onSave={setPolicy} /></div>
        <div hidden={view !== "functions"}><AgentFunctionList agent={agent} /></div>
      </div>
    </>}
    {policy && <Modal title={`Update ${agent?.name ?? agentId} policy?`} onClose={() => { if (!saving) setPolicy(null); }}>
      <p>This changes which tools the agent can discover and call on subsequent turns. Backend runtime checks and action approvals still apply.</p>
      {error && <p className="error-text" role="alert">{error}</p>}
      <div className="button-row"><button type="button" disabled={saving} onClick={async () => {
        setSaving(true); setError(null);
        try { setAgent(await api.updateAgentPolicy(agentId, policy)); setPolicy(null); }
        catch (reason) { setError(reason instanceof Error ? reason.message : "Could not update policy"); }
        finally { setSaving(false); }
      }}>{saving ? "Saving…" : "Update policy"}</button><button type="button" className="secondary" disabled={saving} onClick={() => setPolicy(null)}>Cancel</button></div>
    </Modal>}
  </section>;
}
