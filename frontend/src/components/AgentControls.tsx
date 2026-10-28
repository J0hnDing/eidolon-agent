import { FormEvent, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { AgentDefinition, AgentPolicy, AssistantRuntime, AssistantRuntimeUpdate, AssistantProposal } from "../api/client";
import { parseBackendDateTime } from "../lib/dateTime";
import "./agent-configuration.css";

export function AgentPolicyEditor({ agent, isWorking, onSave }: {
  agent: AgentDefinition;
  isWorking: boolean;
  onSave: (policy: Omit<AgentPolicy, "model" | "reasoning_effort">) => void;
}) {
  const [policy, setPolicy] = useState(agent.policy);
  const [allowedText, setAllowedText] = useState(agent.policy.allowed_functions.join(", "));
  const [bannedText, setBannedText] = useState(agent.policy.banned_functions.join(", "));

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onSave({
      max_risk: policy.max_risk,
      read_only: policy.read_only,
      allowed_functions: parseFunctionList(allowedText),
      banned_functions: parseFunctionList(bannedText),
    });
  }

  return (
    <form className="form-panel stack agent-policy-editor" onSubmit={submit}>
      <div><h2>Permission policy</h2><p className="muted">The backend applies this policy to tool discovery and checks it again for every call.</p></div>
      <section className="agent-policy-section" aria-labelledby="agent-function-defaults-title">
        <div><h3 id="agent-function-defaults-title">Function defaults</h3><p className="muted">Set the broad risk limit and whether functions must be read-only.</p></div>
        <div className="agent-policy-fields">
          <label>Maximum function risk
            <select value={policy.max_risk} onChange={(event) => setPolicy({ ...policy, max_risk: event.target.value as AgentPolicy["max_risk"] })}>
              <option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option>
            </select>
          </label>
          <label className="checkbox-row agent-policy-checkbox">
            <input type="checkbox" checked={policy.read_only} onChange={(event) => setPolicy({ ...policy, read_only: event.target.checked })} />
            Restrict functions to read-only operations by default
          </label>
        </div>
      </section>
      <section className="agent-policy-section" aria-labelledby="agent-function-overrides-title">
        <div><h3 id="agent-function-overrides-title">Function overrides</h3><p className="muted">Enter function IDs separated by commas or new lines. Bans take precedence; explicit allows can override the risk and read-only defaults.</p></div>
        <div className="agent-function-overrides">
          <label>Explicitly allowed function IDs
            <textarea rows={3} value={allowedText} onChange={(event) => setAllowedText(event.target.value)} placeholder="function.one, function.two" />
          </label>
          <label>Explicitly banned function IDs
            <textarea rows={3} value={bannedText} onChange={(event) => setBannedText(event.target.value)} placeholder="function.three" />
          </label>
        </div>
      </section>
      <p className="muted agent-policy-approval-note">These settings do not bypass function availability or runtime approval.</p>
      <div className="button-row"><button type="submit" disabled={isWorking}>Save policy</button></div>
    </form>
  );
}

export function AgentFunctionList({ agent }: { agent: AgentDefinition }) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<"all" | "allowed" | "blocked">("all");
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const visibleFunctions = agent.functions.filter((item) => {
    if (filter === "allowed" && !item.allowed) return false;
    if (filter === "blocked" && item.allowed) return false;
    if (!normalizedQuery) return true;
    return [item.title, item.id, item.description, item.reason, item.risk_level]
      .some((value) => value.toLocaleLowerCase().includes(normalizedQuery));
  });
  return (
    <section className="detail-panel stack agent-function-visibility" aria-labelledby="agent-effective-functions-title">
      <div><h2 id="agent-effective-functions-title">Effective functions</h2><p className="muted">Current visibility after policy, availability, and private-tool rules.</p></div>
      <div className="agent-function-filters">
        <label>Search functions
          <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Name, ID, or description" />
        </label>
        <label>Show
          <select value={filter} onChange={(event) => setFilter(event.target.value as typeof filter)}>
            <option value="all">All functions</option><option value="allowed">Allowed</option><option value="blocked">Blocked</option>
          </select>
        </label>
      </div>
      <p className="muted agent-function-count" aria-live="polite">Showing {visibleFunctions.length} of {agent.functions.length} functions</p>
      <div className="agent-function-list">
        {visibleFunctions.map((item) => (
          <article key={item.id} className="agent-function-row">
            <div><strong>{item.title}</strong><code>{item.id}</code></div>
            <span className={`badge status-${item.allowed ? "active" : "paused"}`}>{item.allowed ? "allowed" : "blocked"}</span>
            <p>{item.description}</p>
            <small>{item.risk_level} risk · {item.mcp_read_only ? "read-only" : "may write"} · {item.reason}</small>
          </article>
        ))}
        {!agent.functions.length && <p className="muted">No functions are visible to this agent.</p>}
        {agent.functions.length > 0 && !visibleFunctions.length && <p className="muted">No functions match these filters.</p>}
      </div>
    </section>
  );
}

export function AssistantRuntimeSettings({ runtime, isWorking, onSave, onRefresh }: {
  runtime: AssistantRuntime;
  isWorking: boolean;
  onSave: (payload: AssistantRuntimeUpdate) => Promise<void>;
  onRefresh: () => Promise<void>;
}) {
  const [draft, setDraft] = useState(runtime);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { if (!dirty) setDraft(runtime); }, [runtime, dirty]);
  function change(payload: AssistantRuntimeUpdate) { setDraft((current) => ({ ...current, ...payload })); setDirty(true); }
  return <section className="detail-panel assistant-runtime-settings" aria-labelledby="assistant-runtime-settings-title">
    <div><h2 id="assistant-runtime-settings-title">Proactive Assistant and session</h2></div>
    <form className="stack" onSubmit={async (event) => {
      event.preventDefault(); setSaving(true); setError(null);
      try {
        await onSave({ enabled: draft.enabled, timezone: draft.timezone });
        setDirty(false);
      } catch (reason) { setError(errorMessage(reason, "Could not save Assistant settings")); }
      finally { setSaving(false); }
    }}>
      <p className="muted">Morning planning, evening reflection, and other scheduled tasks are managed in the agenda. After time offline, each recurring task recovers only its most recently missed occurrence.</p>
      <div className="form-grid">
        <label className="checkbox-row"><input type="checkbox" checked={draft.enabled} onChange={(event) => change({ enabled: event.target.checked })} />Enable proactive Assistant</label>
        <label>Assistant timezone<input value={draft.timezone} required onChange={(event) => change({ timezone: event.target.value })} /></label>
      </div>
      <p className="muted">The Assistant starts a fresh session daily at {formatClockTime(runtime.refresh_time ?? "04:00")} ({runtime.timezone}). If Eidolon is offline then, startup recovery applies one missed refresh before the next Assistant turn. The chat stays in place, and previous chat history is not sent into the fresh session.</p>
      {runtime.next_refresh_at && <p className="muted">Next session refresh: {formatTimestamp(runtime.next_refresh_at, runtime.timezone)} ({runtime.timezone})</p>}
      {runtime.thread_started_at && <p className="muted">Current session started: {formatTimestamp(runtime.thread_started_at)}</p>}
      {runtime.refresh_pending && <p className="muted" role="status">Refresh pending. It will be applied before the next turn (after any running turn finishes). The Assistant will start with fresh context; this permanent chat history stays visible but is not sent into the new session.</p>}
      {error && <p className="error-text" role="alert">{error}</p>}
      <div className="button-row"><button type="submit" disabled={isWorking || saving || !dirty}>Save Assistant settings</button>
        <button type="button" className="secondary" onClick={async () => {
          setSaving(true); setError(null);
          try { await onRefresh(); }
          catch (reason) { setError(errorMessage(reason, "Could not refresh the Assistant session")); }
          finally { setSaving(false); }
        }} disabled={isWorking || saving || Boolean(runtime.refresh_pending)}>Refresh Assistant session</button>
      </div>
    </form>
  </section>;
}

export function AssistantControls({ proposals, isWorking, onApprove, onDeny, onEditFunctions }: {
  proposals: AssistantProposal[];
  isWorking: boolean;
  onApprove: (proposalId: number) => void;
  onDeny: (proposalId: number) => Promise<void>;
  onEditFunctions: (proposalId: number, steps: Array<{ function: string; arguments: Record<string, unknown> }>) => Promise<void>;
}) {
  return (
    <div className="assistant-controls">
      <section className="detail-panel stack">
        <div><h2>Proposed actions</h2><p className="muted">Approval runs the shown function sequence or sends an instruction to a linked Act session.</p></div>
        <div className="assistant-proposal-list">
          {proposals.map((proposal) => (
            <article key={proposal.id} className="assistant-proposal">
              <header><div><strong>{proposal.title}</strong><small>{formatTimestamp(proposal.created_at)}</small></div><span className={`badge status-${proposal.status}`}>{proposal.status}</span></header>
              {proposal.rationale && <p>{proposal.rationale}</p>}
              {proposal.actions && <p><strong>Actions:</strong> {proposal.actions}</p>}
              {proposal.references.length > 0 && <p className="muted">References: {proposal.references.join(", ")}</p>}
              {proposal.action?.type === "functions" ? <details><summary>Function sequence</summary><pre>{JSON.stringify(proposal.action.steps, null, 2)}</pre></details> : <details><summary>Act instruction</summary><pre>{proposal.action?.type === "act" ? proposal.action.instruction : proposal.instruction}</pre></details>}
              {proposal.status === "pending" && proposal.action?.type === "functions" && <FunctionSequenceEditor proposal={proposal} disabled={isWorking} onSave={onEditFunctions} />}
              {proposal.follow_up && <p className="muted">Follow-up: {proposal.follow_up.message}</p>}
              {proposal.act_session_id !== null && <Link to={`/agents/act?session=${proposal.act_session_id}`}>Open linked Act session</Link>}
              {proposal.execution_status && <p className="muted">Execution: {proposal.execution_status}</p>}
              {proposal.status === "pending" && <div className="button-row"><button type="button" onClick={() => onApprove(proposal.id)} disabled={isWorking}>Approve action</button><button type="button" className="secondary" onClick={() => onDeny(proposal.id)} disabled={isWorking}>Deny</button></div>}
            </article>
          ))}
          {!proposals.length && <p className="muted">No actions are awaiting review.</p>}
        </div>
      </section>
    </div>
  );
}

function FunctionSequenceEditor({ proposal, disabled, onSave }: {
  proposal: AssistantProposal;
  disabled: boolean;
  onSave: (proposalId: number, steps: Array<{ function: string; arguments: Record<string, unknown> }>) => Promise<void>;
}) {
  const [draft, setDraft] = useState(JSON.stringify(proposal.action?.type === "functions" ? proposal.action.steps : [], null, 2));
  const [error, setError] = useState<string | null>(null);
  return <details><summary>Edit function sequence</summary><form onSubmit={async (event) => {
    event.preventDefault();
    try {
      const parsed: unknown = JSON.parse(draft);
      if (!Array.isArray(parsed) || !parsed.every((step) =>
        step !== null && typeof step === "object" && typeof step.function === "string" &&
        step.arguments !== null && typeof step.arguments === "object" && !Array.isArray(step.arguments))) {
        throw new Error("Enter an array of function calls with object arguments.");
      }
      setError(null);
      await onSave(proposal.id, parsed as Array<{ function: string; arguments: Record<string, unknown> }>);
    } catch (reason) {
      setError(errorMessage(reason, "Enter valid JSON function steps"));
    }
  }}><textarea aria-label="Function sequence JSON" value={draft} onChange={(event) => setDraft(event.target.value)} rows={8} disabled={disabled} /><button type="submit" disabled={disabled}>Save sequence</button>{error && <p className="error-text">{error}</p>}</form></details>;
}

function parseFunctionList(value: string) { return [...new Set(value.split(/[\n,]/).map((item) => item.trim()).filter(Boolean))]; }
function errorMessage(reason: unknown, fallback: string) { return reason instanceof Error ? reason.message : fallback; }
function formatTimestamp(value: string | null | undefined, timeZone?: string) { if (!value) return "Not scheduled"; const date = parseBackendDateTime(value); return Number.isNaN(date.getTime()) ? "Unknown" : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short", timeZone }).format(date); }
function formatClockTime(value: string) {
  const match = /^(\d{2}):(\d{2})$/.exec(value);
  if (!match) return value;
  const hour = Number(match[1]);
  const minute = Number(match[2]);
  if (hour > 23 || minute > 59) return value;
  return `${hour % 12 || 12}:${String(minute).padStart(2, "0")} ${hour < 12 ? "AM" : "PM"}`;
}

