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
  const permissions = summarizePermissions(request);
  const blockedReasons = stringList(request.reason_json.blocked_reasons);
  const unsupportedReasons = stringList(request.reason_json.runner_unsupported);
  const expansion = request.reason_json.permission_expansion;
  const hasExpansion = isNonEmptyObject(expansion);

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <section className="modal-panel permission-modal">
        <header className="page-header">
          <div>
            <p className="eyebrow">
              {request.request_scope === "build_time" ? "Build-Time Approval" : "Runtime Approval"}
            </p>
            <h2>{title}</h2>
            {subject && <p className="muted">{subject}</p>}
          </div>
          <span className={`badge risk-${request.risk_level}`}>{request.risk_level}</span>
        </header>

        <p>{request.user_explanation || request.reason}</p>

        <div className="permission-summary">
          <section>
            <h3>Requested Permissions</h3>
            <ChipList values={permissions.length ? permissions : ["No special runtime permissions"]} />
          </section>
          <section>
            <h3>Network</h3>
            <ChipList values={request.requested_network_domains_json.length ? request.requested_network_domains_json : ["None"]} />
          </section>
          <section>
            <h3>Packages</h3>
            <ChipList values={request.requested_dependencies_json.length ? request.requested_dependencies_json : ["None"]} />
          </section>
          <section>
            <h3>Filesystem</h3>
            <ChipList values={filesystemSummary(request.requested_filesystem_json)} />
          </section>
        </div>

        {(blockedReasons.length > 0 || unsupportedReasons.length > 0 || hasExpansion) && (
          <section className="permission-warning">
            <h3>Needs Attention</h3>
            {hasExpansion && <p>Permission expansion was detected compared with the original plan.</p>}
            {[...blockedReasons, ...unsupportedReasons].map((reason) => (
              <p key={reason}>{reason}</p>
            ))}
          </section>
        )}

        <section className="permission-limits">
          <h3>What Approval Means</h3>
          <p>
            Generation approval only lets Codex create proposed files. Runtime approval only records consent for the
            generated manifest. Skills are never installed or run automatically.
          </p>
        </section>

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

function summarizePermissions(request: ApprovalRequest): string[] {
  const permissions = request.requested_permissions_json;
  const labels: string[] = [];
  if (permissions.codex_generation) labels.push("Codex generation");
  if (permissions.internet_research) labels.push("Internet research during generation");
  const runtime = permissions.future_runtime_permissions;
  if (isRecord(runtime)) {
    addRuntimePermissionLabels(labels, runtime);
  } else {
    addRuntimePermissionLabels(labels, permissions);
  }
  return Array.from(new Set(labels));
}

function addRuntimePermissionLabels(labels: string[], permissions: Record<string, unknown>) {
  if (arrayLength(permissions.network) > 0) labels.push("Network");
  if (arrayLength(permissions.filesystem_read) > 0) labels.push("Filesystem read");
  if (arrayLength(permissions.filesystem_write) > 0) labels.push("Filesystem write");
  if (arrayLength(permissions.secrets) > 0) labels.push("Secrets");
  if (permissions.shell === true) labels.push("Shell");
}

function filesystemSummary(value: Record<string, unknown>): string[] {
  const rows: string[] = [];
  const reads = stringList(value.filesystem_read);
  const writes = stringList(value.filesystem_write);
  if (reads.length) rows.push(`Read: ${reads.join(", ")}`);
  if (writes.length) rows.push(`Write: ${writes.join(", ")}`);
  return rows.length ? rows : ["None"];
}

function ChipList({ values }: { values: string[] }) {
  return (
    <div className="chip-list">
      {values.map((value) => (
        <span key={value} className="permission-chip">
          {value}
        </span>
      ))}
    </div>
  );
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item));
}

function arrayLength(value: unknown): number {
  return Array.isArray(value) ? value.length : 0;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNonEmptyObject(value: unknown): boolean {
  return isRecord(value) && Object.keys(value).length > 0;
}
