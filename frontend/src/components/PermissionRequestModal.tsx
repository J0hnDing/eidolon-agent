import { ApprovalRequest } from "../api/client";

export default function PermissionRequestModal({
  request,
  title,
  subject,
  approveLabel = "Approve",
  denyLabel = "Decline",
  isWorking,
  onApprove,
  onDeny,
}: {
  request: ApprovalRequest;
  title: string;
  subject?: string;
  approveLabel?: string;
  denyLabel?: string;
  isWorking: boolean;
  onApprove: () => void;
  onDeny: () => void;
}) {
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <section className="modal-panel">
        <header className="page-header">
          <div>
            <p className="eyebrow">{request.request_scope === "build_time" ? "Build-Time Approval" : "Runtime Approval"}</p>
            <h2>{title}</h2>
            {subject && <p className="muted">{subject}</p>}
          </div>
          <span className={`badge risk-${request.risk_level}`}>{request.risk_level}</span>
        </header>

        <p>{request.user_explanation || request.reason}</p>

        <div className="modal-grid">
          <PermissionBlock title="Requested Permissions" value={request.requested_permissions_json} />
          <PermissionBlock title="Network Domains" value={request.requested_network_domains_json} />
          <PermissionBlock title="Dependencies" value={request.requested_dependencies_json} />
          <PermissionBlock title="Filesystem Access" value={request.requested_filesystem_json} />
          <PermissionBlock title="Reasons and Limits" value={request.reason_json} />
        </div>

        <p className="muted">
          Approving generation only allows proposed files to be created. Approving runtime permissions only records
          consent for the declared manifest permissions. Skills are never installed or run automatically.
        </p>

        <div className="button-row">
          <button type="button" onClick={onApprove} disabled={isWorking || request.risk_level === "blocked"}>
            {isWorking ? "Working..." : approveLabel}
          </button>
          <button type="button" className="secondary" onClick={onDeny} disabled={isWorking}>
            {denyLabel}
          </button>
        </div>
      </section>
    </div>
  );
}

function PermissionBlock({ title, value }: { title: string; value: unknown }) {
  return (
    <div>
      <h3>{title}</h3>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </div>
  );
}
