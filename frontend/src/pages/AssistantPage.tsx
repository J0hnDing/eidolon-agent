import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { AssistantAgendaItem, AssistantProposal, AssistantRuntime, AssistantRuntimeUpdate, api } from "../api/client";
import { AgentConfiguration } from "../components/AgentConfiguration";
import { AgentInstructions, AgentModelSettings } from "../components/AgentSettings";
import { AssistantControls, AssistantRuntimeSettings } from "../components/AgentControls";
import { AssistantAgenda } from "../components/AssistantAgenda";
import { Modal } from "../components/Modal";
import { AppIcon } from "../components/AppIcon";
import { usePolling } from "../lib/usePolling";
import { parseBackendDateTime } from "../lib/dateTime";
import ChatPage from "./ChatPage";
import "./assistant.css";

export default function AssistantPage() {
  return <section className="page stack assistant-page">
    <header className="page-header conversation-page-header"><h1>Assistant</h1>
      <Link className="button-link secondary compact" to="/assistant/details"><AppIcon name="panel" size={16} />Assistant details</Link>
    </header>
    <div className="assistant-conversation"><ChatPage surface="assistant" /></div>
  </section>;
}

export function AssistantDetailsPage() {
  const [params] = useSearchParams();
  const view = ["actions", "settings", "access"].includes(params.get("view") ?? "") ? params.get("view")! : "agenda";
  const [visited, setVisited] = useState(() => new Set([view]));
  useEffect(() => { setVisited((previous) => new Set([...previous, view])); }, [view]);
  const [runtime, setRuntime] = useState<AssistantRuntime | null>(null);
  const [agenda, setAgenda] = useState<AssistantAgendaItem[]>([]);
  const [proposals, setProposals] = useState<AssistantProposal[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(false);
  const [approving, setApproving] = useState<number | null>(null);
  const [selectedProposalId, setSelectedProposalId] = useState<number | null>(null);
  const [actionQuery, setActionQuery] = useState("");
  const [actionFilter, setActionFilter] = useState("pending");

  async function load() {
    const results = await Promise.allSettled([api.getAssistantRuntime(), api.listAssistantAgenda(), api.listAssistantProposals()]);
    const [runtimeResult, agendaResult, proposalResult] = results;
    if (runtimeResult.status === "fulfilled") setRuntime(runtimeResult.value);
    if (agendaResult.status === "fulfilled") setAgenda(agendaResult.value);
    if (proposalResult.status === "fulfilled") setProposals(proposalResult.value);
    const failed = ["Assistant status", "Agenda", "Proposed actions"].filter((_, index) => results[index].status === "rejected");
    setError(failed.length ? `${failed.join(", ")} could not be refreshed. Try again.` : null);
    setLoading(false);
  }
  useEffect(() => { void load(); }, []);
  usePolling(load, true, 5000);

  async function mutate(action: () => Promise<unknown>) {
    setWorking(true); setError(null);
    try {
      await action();
      window.dispatchEvent(new Event("eidolon:approvals-changed"));
      await load();
      return true;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not complete this action");
      return false;
    } finally { setWorking(false); }
  }
  async function saveRuntime(payload: AssistantRuntimeUpdate) { setRuntime(await api.updateAssistantRuntime(payload)); }
  const pending = proposals.filter((proposal) => proposal.status === "pending");
  const currentWork = proposals.filter((proposal) => proposal.execution_status && !["completed", "succeeded", "failed", "cancelled"].includes(proposal.execution_status));
  const selectedProposal = proposals.find((proposal) => proposal.id === selectedProposalId);
  const visibleProposals = proposals.filter((proposal) =>
    (actionFilter === "all" || (actionFilter === "active" ? currentWork.includes(proposal) : proposal.status === "pending")) &&
    `${proposal.title} ${proposal.rationale} ${proposal.actions}`.toLowerCase().includes(actionQuery.trim().toLowerCase()));

  return <section className="page stack assistant-details-page">
    <header className="page-header"><h1>Assistant details</h1><Link className="button-link secondary compact" to="/assistant">Back to conversation <AppIcon name="arrow" size={15} /></Link></header>
    <nav className="page-tabs assistant-detail-nav" aria-label="Assistant details sections">
      <Link to="?view=agenda" aria-current={view === "agenda" ? "page" : undefined}>Agenda &amp; questions</Link>
      <Link to="?view=actions" aria-current={view === "actions" ? "page" : undefined}>Actions {pending.length > 0 && <span className="assistant-section-count">{pending.length}</span>}</Link>
      <Link to="?view=settings" aria-current={view === "settings" ? "page" : undefined}>Settings</Link>
      <Link to="?view=access" aria-current={view === "access" ? "page" : undefined}>Tool access</Link>
    </nav>
    {loading && <p className="muted" role="status">Loading Assistant details…</p>}
    {error && <div className="error-text" role="alert">{error} <button className="secondary compact" type="button" onClick={() => void load()}>Retry</button></div>}
    {!loading && <div className="assistant-details-content stack">
        <div hidden={view !== "agenda"}>{(visited.has("agenda") || view === "agenda") && <AssistantAgenda items={agenda} questionAsking={runtime?.question_asking} dailyPlan={runtime?.daily_plan} onChange={async () => { setAgenda(await api.listAssistantAgenda()); setRuntime(await api.getAssistantRuntime()); }} />}</div>
        <section hidden={view !== "actions"} className="assistant-actions-section" aria-labelledby="assistant-actions-title">
          <div className="section-heading"><div><h2 id="assistant-actions-title">Actions</h2><p className="muted">Review proposed work and follow its progress.</p></div><Link className="button-link secondary compact" to="/activity">View all activity ↗</Link></div>
          <div className="assistant-actions-toolbar">
            <label className="assistant-action-search"><AppIcon name="search" size={16} /><input aria-label="Search actions" placeholder="Search actions…" value={actionQuery} onChange={(event) => setActionQuery(event.target.value)} /></label>
            <select aria-label="Filter actions" value={actionFilter} onChange={(event) => setActionFilter(event.target.value)}><option value="pending">Awaiting review</option><option value="active">In progress</option><option value="all">All actions</option></select>
            <span className="muted">{visibleProposals.length} {visibleProposals.length === 1 ? "action" : "actions"}</span>
          </div>
          <div className="assistant-action-list">
            <div className="assistant-action-list-heading" aria-hidden="true"><span>Action</span><span>Status</span><span>Created</span><span /></div>
            {visibleProposals.map((proposal) => <button type="button" key={proposal.id} className="assistant-action-row" onClick={() => setSelectedProposalId(proposal.id)}>
              <span><strong>{proposal.title}</strong><small>{proposal.action?.type === "functions" ? "Function sequence" : "Act task"}</small></span>
              <span className={`badge status-${proposal.execution_status || proposal.status}`}>{(proposal.execution_status || proposal.status).replace(/_/g, " ")}</span>
              <time dateTime={proposal.created_at}>{parseBackendDateTime(proposal.created_at).toLocaleDateString(undefined, { month: "short", day: "numeric" })}</time><span aria-hidden="true">→</span>
            </button>)}
            {!visibleProposals.length && <div className="assistant-actions-empty"><h3>{actionQuery ? "No matching actions" : actionFilter === "active" ? "No work in progress" : "No actions to review"}</h3><p className="muted">{actionQuery ? "Try another search or change the filter." : "Proposed work and its progress will appear here."}</p>{(actionQuery || actionFilter !== "all") && <button className="secondary compact" onClick={() => { setActionQuery(""); setActionFilter("all"); }}>Show all actions</button>}</div>}
          </div>
        </section>
      <section hidden={view !== "settings"} className="assistant-settings-section" aria-labelledby="assistant-settings-title">
        <h2 id="assistant-settings-title">Assistant settings</h2>
        {(visited.has("settings") || view === "settings") && <><AgentModelSettings agentId="assistant" /><AgentInstructions agentId="assistant" /></>}
        {runtime && (visited.has("settings") || view === "settings") && <>
          <section className="detail-panel stack"><h3>Assistant status</h3><p className="muted" role="status">Initial Atlas review: {{ pending: "Waiting to start", reading: "Reading Atlas", blocked: "Waiting to retry", complete: "Complete" }[runtime.initialization.status]}{runtime.initialization.reviewed_batches > 0 ? ` · ${runtime.initialization.reviewed_batches} batches reviewed` : ""}{runtime.initialization.remaining !== null && runtime.initialization.status !== "complete" ? ` · ${runtime.initialization.remaining} steps remaining` : ""}{runtime.initialization.status === "blocked" ? ". Progress is saved; the Assistant will retry automatically." : ""}</p><p className="muted">Proactive Assistant {runtime.enabled ? "enabled" : "paused"} · {runtime.timezone}</p></section>
          <AssistantRuntimeSettings runtime={runtime} isWorking={working} onSave={saveRuntime}
            onRefresh={async () => { if (!await mutate(async () => { const result = await api.refreshAssistantRuntime(); setRuntime({ ...result, refresh_pending: result.refresh_pending ?? result.status === "refresh_pending" }); })) throw new Error("Could not refresh the Assistant session. Try again."); }} />
        </>}
      </section>
      <section hidden={view !== "access"} className="assistant-access-section" aria-labelledby="assistant-access-title"><h2 id="assistant-access-title">Tool access</h2><p className="muted">Manage the Assistant's permissions and available functions.</p>{(visited.has("access") || view === "access") && <AgentConfiguration agentId="assistant" />}</section>
    </div>}
    {selectedProposal && approving === null && <Modal title={selectedProposal.title} className="assistant-action-detail" onClose={() => { if (!working) setSelectedProposalId(null); }}>
      {error && <p className="error-text" role="alert">{error}</p>}
      <AssistantControls proposals={[selectedProposal]} isWorking={working} onApprove={setApproving}
        onDeny={async (id) => { if (await mutate(() => api.denyAssistantProposal(id))) setSelectedProposalId(null); }}
        onEditFunctions={async (id, steps) => { if (!await mutate(() => api.editAssistantFunctionProposal(id, steps))) throw new Error("Could not save function sequence. Your changes are preserved."); }} />
    </Modal>}
    {approving !== null && <Modal title="Approve this plan?" onClose={() => { if (!working) setApproving(null); }}>
      <p>Approval executes the reviewed function sequence or starts Act with the shown instruction. Runtime permission checks still apply.</p>
      {error && <p className="error-text" role="alert">{error}</p>}
      <div className="button-row"><button type="button" disabled={working} onClick={async () => { if (await mutate(() => api.approveAssistantProposal(approving))) setApproving(null); }}>{working ? "Approving…" : "Approve action"}</button><button type="button" className="secondary" disabled={working} onClick={() => setApproving(null)}>Cancel</button></div>
    </Modal>}
  </section>;
}
