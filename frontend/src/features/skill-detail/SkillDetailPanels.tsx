import { Link } from "react-router-dom";

import {
  ApprovalRequest,
  ProposedSkillValidation,
  SkillRun,
  SkillUpdateResponse,
  SkillVersion,
  SkillVersionComparison,
} from "../../api/client";

export type UpdateChatMessage = {
  id: number;
  role: "user" | "assistant";
  content: string;
  kind?: "text" | "thinking" | "build_approval";
  permissionRequest?: ApprovalRequest;
  agentRunId?: number;
  actionStatus?: "pending" | "working" | "approved" | "denied" | "failed";
};

export function parseRunInput(value: string): Record<string, unknown> {
  const trimmed = value.trim();
  if (!trimmed) return {};
  try {
    return JSON.parse(trimmed) as Record<string, unknown>;
  } catch (err) {
    if (err instanceof SyntaxError) {
      throw new Error(
        'Run input must be valid JSON. Use double quotes around property names, like { "hello": "world" }.',
      );
    }
    throw err;
  }
}

export function UpdateSuggestionChat({
  messages,
  isWorking,
  latestResponse,
  onApprove,
  onDeny,
}: {
  messages: UpdateChatMessage[];
  isWorking: boolean;
  latestResponse: SkillUpdateResponse | null;
  onApprove: (message: UpdateChatMessage) => void;
  onDeny: (message: UpdateChatMessage) => void;
}) {
  if (messages.length === 0) return null;
  return (
    <div className="update-chat">
      {messages.map((message) => (
        <article key={message.id} className={`message ${message.role}`}>
          <span>{message.role}</span>
          {message.kind === "thinking" ? (
            <div className="thinking-row">
              <span className="thinking-spinner" aria-hidden="true" />
              <p>{message.content}</p>
            </div>
          ) : message.kind === "build_approval" && message.permissionRequest ? (
            <div className="chat-approval-card">
              <p>{message.content}</p>
              <InlinePermissionSummary request={message.permissionRequest} />
              <div className="button-row">
                <button
                  type="button"
                  onClick={() => onApprove(message)}
                  disabled={isWorking || message.actionStatus === "working" || message.permissionRequest.risk_level === "blocked"}
                >
                  {message.actionStatus === "working" ? "Working..." : "Approve Update Build"}
                </button>
                <button
                  type="button"
                  className="secondary"
                  onClick={() => onDeny(message)}
                  disabled={isWorking || message.actionStatus === "working"}
                >
                  Decline
                </button>
              </div>
              {message.actionStatus && !["pending", "working"].includes(message.actionStatus) && (
                <p className="muted">Status: {message.actionStatus}</p>
              )}
            </div>
          ) : (
            <p>{message.content}</p>
          )}
        </article>
      ))}
      {latestResponse && (
        <p className="muted">
          Agent Run: <Link to={`/agent-runs/${latestResponse.agent_run_id}`}>#{latestResponse.agent_run_id}</Link>
          {latestResponse.version ? ` / Draft version: ${latestResponse.version.version}` : ""}
        </p>
      )}
    </div>
  );
}

function InlinePermissionSummary({ request }: { request: ApprovalRequest }) {
  const pmSummary = reasonText(request, "product_manager_summary");
  const permissionSummary = reasonText(request, "permission_review_summary") || reasonText(request, "security_reviewer_summary");
  return (
    <div className="permission-inline-summary">
      {pmSummary && <section><h3>ProductManager Summary</h3><p>{pmSummary}</p></section>}
      {permissionSummary && <section><h3>Permission Review</h3><p>{permissionSummary}</p></section>}
      <section>
        <h3>Requested Access</h3>
        <div className="chip-list">
          {permissionLabels(request).map((label) => <span key={label} className="permission-chip">{label}</span>)}
        </div>
      </section>
      <p className="muted">Approval creates a draft version only. It does not activate the version or run the skill.</p>
    </div>
  );
}

export function updateResponseText(skillName: string, response: SkillUpdateResponse): string {
  const rows = [`ProductManager update response for ${skillName}:`, response.message, `Status: ${response.status}`];
  if (response.permission_request) rows.push("Review the approval details below to continue.");
  if (response.version) rows.push(`Draft version: ${response.version.version}`);
  return rows.join("\n\n");
}

export function replaceUpdateMessage(
  messages: UpdateChatMessage[],
  messageId: number,
  replacementMessages: UpdateChatMessage[],
): UpdateChatMessage[] {
  return messages.flatMap((message) => (message.id === messageId ? replacementMessages : [message]));
}

export function updateUpdateMessage(
  messages: UpdateChatMessage[],
  messageId: number,
  updater: (message: UpdateChatMessage) => UpdateChatMessage,
): UpdateChatMessage[] {
  return messages.map((message) => (message.id === messageId ? updater(message) : message));
}

function reasonText(request: ApprovalRequest, key: string): string | null {
  const value = request.reason_json[key];
  return typeof value === "string" && value.trim() ? value : null;
}

function permissionLabels(request: ApprovalRequest): string[] {
  const labels = [];
  if (request.requested_permissions_json.codex_generation) labels.push("Codex generation");
  if (request.requested_permissions_json.internet_research) labels.push("Internet research");
  if (request.requested_network_domains_json.length) labels.push(`Network: ${request.requested_network_domains_json.join(", ")}`);
  if (request.requested_dependencies_json.length) labels.push(`Packages: ${request.requested_dependencies_json.join(", ")}`);
  const writes = request.requested_filesystem_json.filesystem_write;
  if (Array.isArray(writes) && writes.length) labels.push(`Write: ${writes.join(", ")}`);
  const reads = request.requested_filesystem_json.filesystem_read;
  if (Array.isArray(reads) && reads.length) labels.push(`Read: ${reads.join(", ")}`);
  return labels.length ? labels : ["No special permissions"];
}

export function isNonEmptyObject(value: unknown): boolean {
  return typeof value === "object" && value !== null && !Array.isArray(value) && Object.keys(value).length > 0;
}

export function VersionList({
  versions,
  activeVersionId,
  isWorking,
  onCompare,
  onActivate,
  onDiscard,
}: {
  versions: SkillVersion[];
  activeVersionId: number | null;
  isWorking: boolean;
  onCompare: (id: number) => void;
  onActivate: (id: number) => void;
  onDiscard: (id: number) => void;
}) {
  if (versions.length === 0) return <p className="muted">No versions have been initialized for this skill yet.</p>;
  return (
    <div className="run-list">
      {versions.map((version) => {
        const isActive = version.id === activeVersionId || version.status === "active";
        const canActivate = !isActive && (version.status === "proposed_update" || version.status === "archived");
        return (
          <article key={version.id} className="run-row">
            <div>
              <strong>{version.version}</strong>
              <span>{version.changelog || version.change_summary || "No changelog provided."}</span>
              <span>Validation: {version.validation_status} / Tests: {version.test_status}</span>
              <span>{version.folder_path}</span>
            </div>
            <div className="button-row">
              <span className={`badge status-${version.status}`}>{version.status}</span>
              <button type="button" className="secondary" onClick={() => onCompare(version.id)} disabled={isWorking}>Compare</button>
              {canActivate && (
                <button type="button" onClick={() => onActivate(version.id)} disabled={isWorking}>
                  {version.status === "archived" ? "Switch Back" : "Activate"}
                </button>
              )}
              {!isActive && <button type="button" className="danger" onClick={() => onDiscard(version.id)} disabled={isWorking}>Delete Version</button>}
            </div>
          </article>
        );
      })}
    </div>
  );
}

export function VersionComparisonPanel({ comparison }: { comparison: SkillVersionComparison }) {
  return (
    <div className="version-compare">
      <h3>Compare {comparison.candidate_version.version} with active {comparison.active_version.version}</h3>
      {comparison.files.map((file) => (
        <details key={file.path} className="run-detail">
          <summary>{file.path}</summary>
          <div className="compare-grid">
            <section><h4>Active</h4><pre>{file.active ?? "File not present."}</pre></section>
            <section><h4>Candidate</h4><pre>{file.candidate ?? "File not present."}</pre></section>
          </div>
        </details>
      ))}
    </div>
  );
}

export function formatTimestamp(value: string | null, fallback: string): string {
  if (!value) return fallback;
  const normalized = /(?:z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`;
  return new Date(normalized).toLocaleString();
}

export function ValidationResult({ validation }: { validation: ProposedSkillValidation }) {
  return (
    <section className="detail-panel">
      <h2>Validation Result</h2>
      <dl className="detail-grid">
        <div><dt>Status</dt><dd>{validation.ok ? "passed" : "failed"}</dd></div>
        <div><dt>Manifest</dt><dd>{validation.manifest_valid ? "valid" : "invalid"}</dd></div>
        <div><dt>Tests</dt><dd>{validation.tests_run ? (validation.tests_passed ? "passed" : "failed") : "not run"}</dd></div>
      </dl>
      {validation.error_message && <div className="run-detail"><h3>Error</h3><pre>{validation.error_message}</pre></div>}
      {validation.warnings.length > 0 && <div className="run-detail"><h3>Warnings</h3><pre>{validation.warnings.join("\n")}</pre></div>}
      {(validation.stdout || validation.stderr) && (
        <div className="run-detail">
          <h3>Test Output</h3><pre>{validation.stdout || "No stdout captured."}</pre>
          <h3>Test Errors</h3><pre>{validation.stderr || "No stderr captured."}</pre>
        </div>
      )}
    </section>
  );
}

export function RunDetail({ run }: { run: SkillRun }) {
  return (
    <div className="run-detail">
      <dl className="detail-grid">
        <div><dt>Status</dt><dd>{run.status}</dd></div>
        <div><dt>Exit Code</dt><dd>{run.exit_code ?? "none"}</dd></div>
        <div><dt>Started</dt><dd>{formatTimestamp(run.started_at, "not started")}</dd></div>
        <div><dt>Ended</dt><dd>{formatTimestamp(run.ended_at, "not ended")}</dd></div>
        <div><dt>Runtime Tokens</dt><dd>{formatTokens(run.total_tokens)}</dd></div>
        <div><dt>Codex Calls</dt><dd>{run.codex_invocations_json.length}</dd></div>
      </dl>
      {run.total_tokens > 0 && <p className="muted">{formatTokens(run.input_tokens)} input, {formatTokens(run.cached_input_tokens)} cached input, {formatTokens(run.output_tokens)} output, {formatTokens(run.reasoning_output_tokens)} reasoning output</p>}
      {run.error_message && <div><h3>Error</h3><pre>{run.error_message}</pre></div>}
      {run.output_json && <div><h3>Output JSON</h3><pre>{JSON.stringify(run.output_json, null, 2)}</pre></div>}
      <div><h3>Stdout</h3><pre>{run.stdout || "No stdout captured."}</pre></div>
      <div><h3>Stderr</h3><pre>{run.stderr || "No stderr captured."}</pre></div>
    </div>
  );
}

export function RunOutput({ run }: { run: SkillRun | null }) {
  if (!run) return <p className="muted">Run the function to see its output.</p>;

  return (
    <div className="run-output">
      <div className="run-output-header">
        <span>Run #{run.id}</span>
        <span className={`badge run-${run.status}`}>{run.status}</span>
      </div>
      {run.output_json ? (
        <pre aria-label={`Output for run ${run.id}`}>{JSON.stringify(run.output_json, null, 2)}</pre>
      ) : run.stdout ? (
        <pre aria-label={`Output for run ${run.id}`}>{run.stdout}</pre>
      ) : run.error_message ? (
        <pre aria-label={`Error for run ${run.id}`}>{run.error_message}</pre>
      ) : (
        <p className="muted">This run did not produce output.</p>
      )}
    </div>
  );
}

export function RunHistory({ runs }: { runs: SkillRun[] }) {
  if (runs.length === 0) return <p className="muted">No run history yet.</p>;

  return (
    <div className="run-history-list">
      {runs.map((run) => (
        <article key={run.id} className="run-history-entry">
          <header className="run-history-header">
            <div>
              <h3>Run #{run.id}</h3>
              <span>{formatTimestamp(run.started_at, "not started")}</span>
            </div>
            <span className={`badge run-${run.status}`}>{run.status}</span>
          </header>
          <RunDetail run={run} />
        </article>
      ))}
    </div>
  );
}

export function formatTokens(value: number): string {
  return new Intl.NumberFormat().format(value);
}
