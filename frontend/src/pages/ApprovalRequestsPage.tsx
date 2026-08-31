import { useEffect, useState } from "react";

import { ApprovalRequest, InvocationApproval, api } from "../api/client";
import { usePolling } from "../lib/usePolling";

type ApprovalTab = "permissions" | "invocations";

export default function ApprovalRequestsPage() {
  const [tab, setTab] = useState<ApprovalTab>("permissions");
  const [requests, setRequests] = useState<ApprovalRequest[]>([]);
  const [invocations, setInvocations] = useState<InvocationApproval[]>([]);
  const [selectedPermission, setSelectedPermission] = useState<ApprovalRequest | null>(null);
  const [selectedInvocation, setSelectedInvocation] = useState<InvocationApproval | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isWorking, setIsWorking] = useState(false);

  async function loadRequests() {
    setError(null);
    try {
      const [loadedPermissions, loadedInvocations] = await Promise.all([
        api.listPermissionRequests(),
        api.listInvocationApprovals(),
      ]);
      setRequests(loadedPermissions);
      setInvocations(loadedInvocations);
      setSelectedPermission((current) =>
        loadedPermissions.find((item) => item.id === current?.id) ?? loadedPermissions[0] ?? null,
      );
      setSelectedInvocation((current) =>
        loadedInvocations.find((item) => item.id === current?.id) ?? loadedInvocations[0] ?? null,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load approval requests");
    }
  }

  useEffect(() => { void loadRequests(); }, []);
  usePolling(
    () => loadRequests(),
    requests.some((request) => request.status === "pending") ||
      invocations.some((request) => request.decision_status === "pending" || request.execution_status === "executing"),
    3000,
  );

  async function decidePermission(action: "approve" | "deny") {
    if (!selectedPermission) return;
    setIsWorking(true);
    setError(null);
    try {
      const updated = action === "approve"
        ? await api.approvePermissionRequest(selectedPermission.id)
        : await api.denyPermissionRequest(selectedPermission.id);
      setSelectedPermission(updated);
      await loadRequests();
    } catch (err) {
      setError(err instanceof Error ? err.message : `Could not ${action} request`);
    } finally { setIsWorking(false); }
  }

  async function decideInvocation(action: "approve" | "deny") {
    if (!selectedInvocation) return;
    setIsWorking(true);
    setError(null);
    try {
      const updated = action === "approve"
        ? await api.approveInvocationApproval(selectedInvocation.id)
        : await api.denyInvocationApproval(selectedInvocation.id);
      setSelectedInvocation(updated);
      await loadRequests();
    } catch (err) {
      setError(err instanceof Error ? err.message : `Could not ${action} invocation`);
    } finally { setIsWorking(false); }
  }

  return (
    <section className="page stack">
      <header className="page-header"><div><h1>Approval Requests</h1><p className="muted">Review access grants and individual actions without mixing their consent boundaries.</p></div></header>
      <div className="approval-tabs" role="tablist" aria-label="Approval type">
        <button type="button" role="tab" aria-selected={tab === "permissions"} className={tab === "permissions" ? "" : "secondary"} onClick={() => setTab("permissions")}>Permission approvals</button>
        <button type="button" role="tab" aria-selected={tab === "invocations"} className={tab === "invocations" ? "" : "secondary"} onClick={() => setTab("invocations")}>Invocation approvals</button>
      </div>
      {error && <p className="error-text">{error}</p>}
      {tab === "permissions" ? <PermissionApprovals requests={requests} selected={selectedPermission} isWorking={isWorking} onSelect={setSelectedPermission} onDecide={decidePermission} /> : <InvocationApprovals requests={invocations} selected={selectedInvocation} isWorking={isWorking} onSelect={setSelectedInvocation} onDecide={decideInvocation} />}
    </section>
  );
}

function PermissionApprovals({ requests, selected, isWorking, onSelect, onDecide }: { requests: ApprovalRequest[]; selected: ApprovalRequest | null; isWorking: boolean; onSelect: (request: ApprovalRequest) => void; onDecide: (action: "approve" | "deny") => void }) {
  return <section className="section-grid"><div className="detail-panel request-browser"><h2>Requests</h2><div className="run-list">
    {requests.map((request) => <button key={request.id} type="button" className={`request-option ${selected?.id === request.id ? "active" : ""}`} onClick={() => onSelect(request)} aria-pressed={selected?.id === request.id}><span><strong>#{request.id} {request.request_scope}</strong><small>{request.request_type}</small></span><span className={`badge status-${request.status}`}>{request.status}</span></button>)}
    {requests.length === 0 && <p className="muted">No permission approval requests yet.</p>}
  </div></div><div className="detail-panel"><h2>Details</h2>{selected ? <div className="run-detail">
    <dl className="detail-grid"><div><dt>Scope</dt><dd>{selected.request_scope}</dd></div><div><dt>Status</dt><dd><span className={`badge status-${selected.status}`}>{selected.status}</span></dd></div><div><dt>Risk</dt><dd><span className={`badge risk-${selected.risk_level}`}>{selected.risk_level}</span></dd></div><div><dt>Type</dt><dd>{selected.request_type}</dd></div></dl>
    <p>{selected.user_explanation || selected.reason}</p><pre>{JSON.stringify(selected, null, 2)}</pre><div className="button-row"><button type="button" onClick={() => onDecide("approve")} disabled={isWorking || selected.status !== "pending" || selected.risk_level === "blocked"}>Approve</button><button type="button" className="secondary" onClick={() => onDecide("deny")} disabled={isWorking || selected.status !== "pending"}>Deny</button></div>
  </div> : <p className="muted">Select a permission request.</p>}</div></section>;
}

function InvocationApprovals({ requests, selected, isWorking, onSelect, onDecide }: { requests: InvocationApproval[]; selected: InvocationApproval | null; isWorking: boolean; onSelect: (request: InvocationApproval) => void; onDecide: (action: "approve" | "deny") => void }) {
  const [filter, setFilter] = useState<"all" | "pending" | "history">("all");
  const filtered = requests.filter((request) => filter === "all" || (filter === "pending" ? request.decision_status === "pending" : request.decision_status !== "pending"));
  return <section className="section-grid"><div className="detail-panel request-browser"><h2>Actions</h2><label>Show<select value={filter} onChange={(event) => setFilter(event.target.value as typeof filter)}><option value="all">All</option><option value="pending">Pending</option><option value="history">History</option></select></label><div className="run-list">
    {filtered.map((request) => <button key={request.id} type="button" className={`request-option ${selected?.id === request.id ? "active" : ""}`} onClick={() => onSelect(request)} aria-pressed={selected?.id === request.id}><span><strong>#{request.id} {request.target_id}</strong><small>{request.reason_to_call}</small></span><span className={`badge status-${request.decision_status}`}>{request.decision_status}</span></button>)}
    {filtered.length === 0 && <p className="muted">No invocation approvals yet.</p>}
  </div></div><div className="detail-panel"><h2>Action preview</h2>{selected ? <div className="run-detail">
    <dl className="detail-grid"><div><dt>Action</dt><dd>{selected.target_id}</dd></div><div><dt>Decision</dt><dd>{selected.decision_status}</dd></div><div><dt>Execution</dt><dd>{selected.execution_status}</dd></div><div><dt>Telegram</dt><dd>{selected.telegram_delivery_status}</dd></div><div><dt>Caller</dt><dd>{selected.source}</dd></div><div><dt>Account</dt><dd>{selected.provider_account_id ?? "Local"}</dd></div></dl>
    <h3>Reason</h3><p>{selected.reason_to_call}</p><InvocationInputPreview request={selected} />{selected.result_json && <><h3>Result</h3><pre>{JSON.stringify(selected.result_json, null, 2)}</pre></>}{selected.error_message && <p className="error-text">{selected.error_message}</p>}
    <div className="button-row"><button type="button" onClick={() => onDecide("approve")} disabled={isWorking || selected.decision_status !== "pending"}>Approve action</button><button type="button" className="secondary" onClick={() => onDecide("deny")} disabled={isWorking || selected.decision_status !== "pending"}>Deny action</button></div>
  </div> : <p className="muted">Select an invocation approval.</p>}</div></section>;
}

function InvocationInputPreview({ request }: { request: InvocationApproval }) {
  const fields = Array.isArray(request.presentation_json?.fields)
    ? request.presentation_json.fields
    : [];
  if (fields.length === 0) {
    return <p className="muted">No backend approval presentation is available.</p>;
  }
  return <>{fields.map((field, index) => field.multiline ? (
    <div key={`${field.label}-${index}`}><h3>{field.label}</h3><pre>{field.value}</pre></div>
  ) : (
    <dl className="detail-grid" key={`${field.label}-${index}`}><div><dt>{field.label}</dt><dd>{field.value}</dd></div></dl>
  ))}</>;
}
