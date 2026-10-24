import { useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { ApprovalRequest, AssistantProposal, InvocationApproval, api } from "../api/client";
import { formatSystemDateTime } from "../lib/dateTime";
import "./records.css";
import { AppIcon } from "../components/AppIcon";
import { usePolling } from "../lib/usePolling";

type ApprovalTab = "permissions" | "invocations" | "assistant";

interface ApprovalRequestsPageProps {
  embedded?: boolean;
}

export default function ApprovalRequestsPage({ embedded = false }: ApprovalRequestsPageProps) {
  const [tab, setTab] = useState<ApprovalTab>("permissions");
  const tabsId = useId();
  const initialTabChosen = useRef(false);
  const [filter, setFilter] = useState<"all" | "pending" | "history">("pending");
  const [query, setQuery] = useState("");
  const [requests, setRequests] = useState<ApprovalRequest[]>([]);
  const [invocations, setInvocations] = useState<InvocationApproval[]>([]);
  const [proposals, setProposals] = useState<AssistantProposal[]>([]);
  const [selectedPermission, setSelectedPermission] = useState<ApprovalRequest | null>(null);
  const [selectedInvocation, setSelectedInvocation] = useState<InvocationApproval | null>(null);
  const [selectedProposal, setSelectedProposal] = useState<AssistantProposal | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isWorking, setIsWorking] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [hasLoaded, setHasLoaded] = useState(false);

  async function loadRequests(options: { showLoading?: boolean } = {}) {
    if (options.showLoading !== false) setIsLoading(true);
    try {
      const [loadedPermissions, loadedInvocations, loadedProposals] = await Promise.all([
        api.listPermissionRequests(),
        api.listInvocationApprovals(),
        api.listAssistantProposals(),
      ]);
      if (!initialTabChosen.current) {
        initialTabChosen.current = true;
        if (!loadedPermissions.some((item) => item.status === "pending")) {
          if (loadedInvocations.some((item) => item.decision_status === "pending")) setTab("invocations");
          else if (loadedProposals.some((item) => item.status === "pending")) setTab("assistant");
        }
      }
      setRequests(loadedPermissions);
      setInvocations(loadedInvocations);
      setProposals(loadedProposals);
      setSelectedPermission((current) =>
        loadedPermissions.find((item) => item.id === current?.id) ?? loadedPermissions[0] ?? null,
      );
      setSelectedInvocation((current) =>
        loadedInvocations.find((item) => item.id === current?.id) ?? loadedInvocations[0] ?? null,
      );
      setSelectedProposal((current) =>
        loadedProposals.find((item) => item.id === current?.id) ?? loadedProposals[0] ?? null,
      );
      setHasLoaded(true);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load approval requests");
    } finally {
      if (options.showLoading !== false) setIsLoading(false);
    }
  }

  useEffect(() => { void loadRequests(); }, []);
  usePolling(
    () => loadRequests({ showLoading: false }),
    requests.some((request) => request.status === "pending") ||
      invocations.some((request) => request.decision_status === "pending" || request.execution_status === "executing") ||
      proposals.some((proposal) => proposal.status === "pending"),
    3000,
  );

  async function decidePermission(action: "approve" | "deny") {
    const selected = permissionRows.find((item) => item.id === selectedPermission?.id) ?? permissionRows[0];
    if (!selected || isWorking) return;
    setIsWorking(true);
    setError(null);
    try {
      const updated = action === "approve"
        ? await api.approvePermissionRequest(selected.id)
        : await api.denyPermissionRequest(selected.id);
      setSelectedPermission(updated);
      notifyApprovalCountChanged();
      await loadRequests();
    } catch (err) {
      setError(err instanceof Error ? err.message : `Could not ${action} request`);
    } finally { setIsWorking(false); }
  }

  async function decideInvocation(action: "approve" | "deny") {
    const selected = invocationRows.find((item) => item.id === selectedInvocation?.id) ?? invocationRows[0];
    if (!selected || isWorking) return;
    setIsWorking(true);
    setError(null);
    try {
      const updated = action === "approve"
        ? await api.approveInvocationApproval(selected.id)
        : await api.denyInvocationApproval(selected.id);
      setSelectedInvocation(updated);
      notifyApprovalCountChanged();
      await loadRequests();
    } catch (err) {
      setError(err instanceof Error ? err.message : `Could not ${action} invocation`);
    } finally { setIsWorking(false); }
  }

  const matches = (status: string, text: string) => {
    const matchesStatus = filter === "all" ||
      (filter === "pending" ? status === "pending" : status !== "pending");
    return matchesStatus && text.toLowerCase().includes(query.trim().toLowerCase());
  };
  const permissionRows = requests.filter((item) => matches(
    item.status,
    `${item.id} ${item.user_explanation} ${item.reason} ${item.request_scope} ${item.request_type}`,
  ));
  const invocationRows = invocations.filter((item) => matches(
    item.decision_status,
    `${item.id} ${item.target_id} ${item.reason_to_call} ${item.source}`,
  ));
  const proposalRows = proposals.filter((item) => matches(
    item.status,
    `${item.title} ${item.rationale} ${item.instruction}`,
  ));
  const approvalTabs = [
    ["permissions", "Permission approvals", requests.filter((item) => item.status === "pending").length],
    ["invocations", "Invocation approvals", invocations.filter((item) => item.decision_status === "pending").length],
    ["assistant", "Assistant proposals", proposals.filter((item) => item.status === "pending").length],
  ] as const;
  const pendingCount = approvalTabs.reduce((total, [, , count]) => total + count, 0);
  const visibleCount = tab === "permissions" ? permissionRows.length
    : tab === "invocations" ? invocationRows.length : proposalRows.length;
  const overviewTitle = isLoading ? "Loading requests"
    : !hasLoaded ? "Requests unavailable"
    : pendingCount ? `${pendingCount} ${pendingCount === 1 ? "decision needs" : "decisions need"} your attention`
    : "You're all caught up";

  const content = (
    <>
      {!embedded && (
        <header className="page-header">
          <div>
            <h1>Approval Requests</h1>
            <p className="muted">Review access grants, individual actions, and Assistant proposals.</p>
          </div>
        </header>
      )}
      <div className="approval-overview-line">
        <div>
          <h2>{overviewTitle}</h2>
          <p className="muted">Inspect each request and decide what Eidolon can do next.</p>
        </div>
        <button
          type="button"
          className="secondary"
          disabled={isLoading || isWorking}
          onClick={() => void loadRequests()}
        >Refresh</button>
      </div>
      <div
        className="approval-tabs record-type-tabs"
        role="tablist"
        aria-label="Approval type"
        onKeyDown={(event) => {
          const tabs: ApprovalTab[] = ["permissions", "invocations", "assistant"];
          const index = tabs.indexOf(tab);
          const next = event.key === "ArrowRight" ? tabs[(index + 1) % tabs.length]
            : event.key === "ArrowLeft" ? tabs[(index + tabs.length - 1) % tabs.length]
            : event.key === "Home" ? tabs[0]
            : event.key === "End" ? tabs[2] : null;
          if (next) {
            event.preventDefault();
            setTab(next);
            document.getElementById(`${tabsId}-${next}`)?.focus();
          }
        }}
      >
        {approvalTabs.map(([id, label, count]) => (
          <button
            key={id}
            id={`${tabsId}-${id}`}
            aria-label={label}
            aria-controls={`${tabsId}-panel`}
            tabIndex={tab === id ? 0 : -1}
            type="button"
            role="tab"
            aria-selected={tab === id}
            onClick={() => setTab(id)}
          >
            {label}
            {count > 0 && <span className="record-tab-count" aria-hidden="true">{count}</span>}
          </button>
        ))}
      </div>
      <div className="records-toolbar approval-inbox-toolbar">
        <div className="record-filter-group" role="group" aria-label="Request status">
          {([["pending", "Needs review"], ["all", "All requests"], ["history", "History"]] as const).map(([id, label]) => (
            <button
              type="button"
              key={id}
              aria-pressed={filter === id}
              className={filter === id ? "active" : ""}
              onClick={() => setFilter(id)}
            >{label}</button>
          ))}
        </div>
        <label className="records-search">
          <AppIcon name="search" size={16} />
          <input
            aria-label="Search approval requests"
            placeholder="Search requests…"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <span className="muted">
          {isLoading || !hasLoaded ? "—" : `${visibleCount} ${visibleCount === 1 ? "request" : "requests"}`}
        </span>
      </div>
      {error && <p className="error-text" role="alert">{error}</p>}
      <div
        id={`${tabsId}-panel`}
        role="tabpanel"
        aria-labelledby={`${tabsId}-${tab}`}
        aria-busy={isLoading}
      >
        {isLoading ? <p className="muted" role="status">Loading approvals...</p>
          : !hasLoaded ? <p className="muted">Refresh to try loading approvals again.</p>
          : tab === "permissions" ? (
            <PermissionApprovals
              requests={permissionRows}
              selected={permissionRows.find((item) => item.id === selectedPermission?.id) ?? permissionRows[0] ?? null}
              isWorking={isWorking}
              onSelect={setSelectedPermission}
              onDecide={decidePermission}
            />
          ) : tab === "invocations" ? (
            <InvocationApprovals
              requests={invocationRows}
              selected={invocationRows.find((item) => item.id === selectedInvocation?.id) ?? invocationRows[0] ?? null}
              isWorking={isWorking}
              onSelect={setSelectedInvocation}
              onDecide={decideInvocation}
            />
          ) : (
            <AssistantProposalApprovals
              requests={proposalRows}
              selected={proposalRows.find((item) => item.id === selectedProposal?.id) ?? proposalRows[0] ?? null}
              onSelect={setSelectedProposal}
            />
          )}
      </div>
    </>
  );
  return embedded
    ? <div className="stack activity-embedded-content approval-workspace">{content}</div>
    : <section className="page stack records-page approval-workspace">{content}</section>;
}

function notifyApprovalCountChanged() {
  window.dispatchEvent(new Event("eidolon:approvals-changed"));
}

function RequestState({ status }: { status: string }) {
  return (
    <span className={`record-request-status status-${status}`}>
      <span className="record-state-dot" aria-hidden="true" />
      {status.replace(/_/g, " ")}
    </span>
  );
}

function InboxEmpty() {
  return (
    <div className="record-quiet-empty">
      <h3>No requests here</h3>
      <p>Try another type, search, or status filter.</p>
    </div>
  );
}

function DetailEmpty() {
  return (
    <div className="approval-empty-detail">
      <span aria-hidden="true">✓</span>
      <h2>Everything in its place</h2>
      <p>Select a request to review its details and approval boundaries.</p>
    </div>
  );
}

function PermissionApprovals({ requests, selected, isWorking, onSelect, onDecide }: {
  requests: ApprovalRequest[];
  selected: ApprovalRequest | null;
  isWorking: boolean;
  onSelect: (request: ApprovalRequest) => void;
  onDecide: (action: "approve" | "deny") => void;
}) {
  return (
    <section className="approval-inbox">
      <div className="approval-request-list" aria-label="Permission requests">
        {requests.map((request) => (
          <button
            key={request.id}
            type="button"
            className={`approval-request-row ${selected?.id === request.id ? "active" : ""}`}
            onClick={() => onSelect(request)}
            aria-pressed={selected?.id === request.id}
          >
            <span className="approval-request-row-top">
              <small>Permission · #{request.id}</small>
              <RequestState status={request.status} />
            </span>
            <strong>{request.user_explanation || request.reason || `${request.request_scope} access`}</strong>
            <span className="approval-request-row-bottom">
              <small>{request.request_scope.replace(/_/g, " ")} · {request.risk_level} risk</small>
              <time dateTime={request.created_at}>{formatSystemDateTime(request.created_at)}</time>
            </span>
          </button>
        ))}
        {requests.length === 0 && <InboxEmpty />}
      </div>
      <div className="approval-detail">
        {selected ? (
          <div className="run-detail">
            <header className="approval-detail-header">
              <div className="approval-detail-breadcrumb">Permission request #{selected.id}</div>
              <h2>Review requested access</h2>
              <p className="approval-request-explanation">{selected.user_explanation || selected.reason}</p>
              <RequestState status={selected.status} />
            </header>
            <dl className="record-properties">
              <div><dt>Access scope</dt><dd>{selected.request_scope.replace(/_/g, " ")}</dd></div>
              <div><dt>Risk</dt><dd><span className={`badge risk-${selected.risk_level}`}>{selected.risk_level}</span></dd></div>
              <div><dt>Request type</dt><dd>{selected.request_type.replace(/_/g, " ")}</dd></div>
              <div><dt>Requested</dt><dd>{formatSystemDateTime(selected.created_at)}</dd></div>
            </dl>
            <section className="approval-detail-section">
              <h3>Why this access is needed</h3>
              <p>{selected.reason || selected.user_explanation}</p>
            </section>
            <PermissionAccessPreview request={selected} />
            {(selected.skill_id !== null || selected.generation_request_id !== null) && (
              <section className="approval-detail-section">
                <h3>Source</h3>
                <p>
                  {selected.skill_id !== null
                    ? <Link to={`/skills/${selected.skill_id}`}>Open Skill #{selected.skill_id}</Link>
                    : `Skill request #${selected.generation_request_id}`}
                </p>
              </section>
            )}
            <details className="record-technical-details">
              <summary>Technical details</summary>
              <pre>{JSON.stringify(selected, null, 2)}</pre>
            </details>
            <div className="approval-decision-bar">
              <p className="muted">Approval applies to the access declared in this request.</p>
              <div className="button-row">
                <button
                  type="button"
                  className="secondary"
                  onClick={() => onDecide("deny")}
                  disabled={isWorking || selected.status !== "pending"}
                >Deny</button>
                <button
                  type="button"
                  onClick={() => onDecide("approve")}
                  disabled={isWorking || selected.status !== "pending" || selected.risk_level === "blocked"}
                >Approve</button>
              </div>
            </div>
          </div>
        ) : <DetailEmpty />}
      </div>
    </section>
  );
}

function InvocationApprovals({ requests, selected, isWorking, onSelect, onDecide }: {
  requests: InvocationApproval[];
  selected: InvocationApproval | null;
  isWorking: boolean;
  onSelect: (request: InvocationApproval) => void;
  onDecide: (action: "approve" | "deny") => void;
}) {
  return (
    <section className="approval-inbox">
      <div className="approval-request-list" aria-label="Invocation requests">
        {requests.map((request) => (
          <button
            key={request.id}
            type="button"
            className={`approval-request-row ${selected?.id === request.id ? "active" : ""}`}
            onClick={() => onSelect(request)}
            aria-pressed={selected?.id === request.id}
          >
            <span className="approval-request-row-top">
              <small>Action · #{request.id}</small>
              <RequestState status={request.decision_status} />
            </span>
            <strong>{request.target_id}</strong>
            <span className="approval-request-row-description">{request.reason_to_call}</span>
            <span className="approval-request-row-bottom">
              <small>{request.source}</small>
              <time dateTime={request.created_at}>{formatSystemDateTime(request.created_at)}</time>
            </span>
          </button>
        ))}
        {requests.length === 0 && <InboxEmpty />}
      </div>
      <div className="approval-detail">
        {selected ? (
          <div className="run-detail">
            <header className="approval-detail-header">
              <div className="approval-detail-breadcrumb">Action request #{selected.id}</div>
              <h2>{selected.target_id}</h2>
              <RequestState status={selected.decision_status} />
            </header>
            <dl className="record-properties">
              <div><dt>Execution</dt><dd>{selected.execution_status.replace(/_/g, " ")}</dd></div>
              <div><dt>Caller</dt><dd>{selected.source}</dd></div>
              <div><dt>Account</dt><dd>{selected.provider_account_id ?? "Local"}</dd></div>
              <div><dt>Telegram delivery</dt><dd>{selected.telegram_delivery_status.replace(/_/g, " ")}</dd></div>
            </dl>
            <section className="approval-detail-section">
              <h3>Reason for this action</h3>
              <p>{selected.reason_to_call}</p>
            </section>
            <section className="approval-detail-section">
              <h3>Action preview</h3>
              <InvocationInputPreview request={selected} />
            </section>
            {(selected.caller_skill_id !== null || selected.target_skill_id !== null || selected.caller_run_id !== null) && (
              <section className="approval-detail-section">
                <h3>Source</h3>
                <p>
                  {selected.caller_run_id !== null && <><span>Skill run #{selected.caller_run_id}</span>{" "}</>}
                  {selected.caller_skill_id !== null && <><Link to={`/skills/${selected.caller_skill_id}`}>Caller skill #{selected.caller_skill_id}</Link>{" "}</>}
                  {selected.target_skill_id !== null && <Link to={`/skills/${selected.target_skill_id}`}>Target skill #{selected.target_skill_id}</Link>}
                </p>
              </section>
            )}
            {selected.result_json && (
              <details className="record-technical-details">
                <summary>Result</summary>
                <pre>{JSON.stringify(selected.result_json, null, 2)}</pre>
              </details>
            )}
            {selected.error_message && <p className="error-text">{selected.error_message}</p>}
            <div className="approval-decision-bar">
              <p className="muted">Approve this individual action after reviewing its preview.</p>
              <div className="button-row">
                <button
                  type="button"
                  className="secondary"
                  onClick={() => onDecide("deny")}
                  disabled={isWorking || selected.decision_status !== "pending"}
                >Deny action</button>
                <button
                  type="button"
                  onClick={() => onDecide("approve")}
                  disabled={isWorking || selected.decision_status !== "pending"}
                >Approve action</button>
              </div>
            </div>
          </div>
        ) : <DetailEmpty />}
      </div>
    </section>
  );
}

function AssistantProposalApprovals({ requests, selected, onSelect }: {
  requests: AssistantProposal[];
  selected: AssistantProposal | null;
  onSelect: (request: AssistantProposal) => void;
}) {
  return (
    <section className="approval-inbox">
      <div className="approval-request-list" aria-label="Assistant proposals">
        {requests.map((proposal) => (
          <button
            key={proposal.id}
            type="button"
            className={`approval-request-row ${selected?.id === proposal.id ? "active" : ""}`}
            onClick={() => onSelect(proposal)}
            aria-pressed={selected?.id === proposal.id}
          >
            <span className="approval-request-row-top">
              <small>Assistant · #{proposal.id}</small>
              <RequestState status={proposal.status} />
            </span>
            <strong>{proposal.title}</strong>
            <span className="approval-request-row-description">{proposal.rationale}</span>
            <span className="approval-request-row-bottom">
              <time dateTime={proposal.created_at}>{formatSystemDateTime(proposal.created_at)}</time>
            </span>
          </button>
        ))}
        {requests.length === 0 && <InboxEmpty />}
      </div>
      <div className="approval-detail">
        {selected ? (
          <div className="run-detail">
            <header className="approval-detail-header">
              <div className="approval-detail-breadcrumb">Assistant proposal #{selected.id}</div>
              <h2>{selected.title}</h2>
              <RequestState status={selected.status} />
            </header>
            <dl className="record-properties">
              <div><dt>Source session</dt><dd>#{selected.source_session_id}</dd></div>
              <div><dt>Created</dt><dd>{formatSystemDateTime(selected.created_at)}</dd></div>
            </dl>
            <section className="approval-detail-section">
              <h3>Why this was proposed</h3>
              <p>{selected.rationale}</p>
            </section>
            <section className="approval-detail-section">
              <h3>Instruction</h3>
              <p className="preserve-whitespace">{selected.instruction}</p>
            </section>
            <section className="approval-detail-section">
              <h3>Proposed actions</h3>
              <p className="preserve-whitespace">{selected.actions}</p>
            </section>
            {selected.references.length > 0 && (
              <section className="approval-detail-section">
                <h3>References</h3>
                <ul>{selected.references.map((reference) => <li key={reference}>{reference}</li>)}</ul>
              </section>
            )}
            <div className="approval-decision-bar">
              <p className="muted">Review and approve this proposal in the Assistant conversation.</p>
              <Link className="button-link secondary" to="/assistant">Open Assistant</Link>
            </div>
          </div>
        ) : <DetailEmpty />}
      </div>
    </section>
  );
}

function PermissionAccessPreview({ request }: { request: ApprovalRequest }) {
  const list = (value: unknown): string[] => Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
  const warnings = [...list(request.reason_json.blocked_reasons), ...list(request.reason_json.runner_unsupported)];
  const means = typeof request.reason_json.approval_means === "string" ? request.reason_json.approval_means : null;
  const excludes = list(request.reason_json.approval_does_not_mean);
  const networkNotice = typeof request.reason_json.runner_network_enforcement === "string"
    ? request.reason_json.runner_network_enforcement
    : null;
  const rows = [
    ["Network domains", request.requested_network_domains_json],
    ["Packages", request.requested_dependencies_json],
    ["Read access", list(request.requested_filesystem_json.filesystem_read)],
    ["Write access", list(request.requested_filesystem_json.filesystem_write)],
  ] as const;
  return (
    <section className="approval-detail-section approval-access-preview">
      <h3>Requested access</h3>
      <dl className="record-properties">
        {rows.map(([label, values]) => (
          <div key={label}><dt>{label}</dt><dd>{values.length ? values.join(", ") : "None"}</dd></div>
        ))}
      </dl>
      {means && <p>{means}</p>}
      {excludes.length > 0 && <p className="muted">This does not approve {excludes.join(", ")}.</p>}
      {warnings.map((warning) => <p className="error-text" key={warning}>{warning}</p>)}
      {networkNotice && <p className="muted">{networkNotice}</p>}
    </section>
  );
}

function InvocationInputPreview({ request }: { request: InvocationApproval }) {
  const fields = Array.isArray(request.presentation_json?.fields)
    ? request.presentation_json.fields
    : [];
  if (fields.length === 0) {
    return <p className="muted">No backend approval presentation is available.</p>;
  }
  return (
    <>
      {fields.map((field, index) => field.multiline ? (
        <div key={`${field.label}-${index}`}><h3>{field.label}</h3><pre>{field.value}</pre></div>
      ) : (
        <dl className="detail-grid" key={`${field.label}-${index}`}>
          <div><dt>{field.label}</dt><dd>{field.value}</dd></div>
        </dl>
      ))}
    </>
  );
}
