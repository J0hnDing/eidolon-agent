import { FormEvent, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import {
  AgentDefinition,
  AgentId,
  AgentPolicy,
  AgentSession,
  AgentSessionSummary,
  AssistantAssessment,
  AssistantProposal,
  api,
} from "../api/client";
import { parseBackendDateTime } from "../lib/dateTime";
import { usePolling } from "../lib/usePolling";

const AGENT_IDS: AgentId[] = ["act", "observer", "assistant"];

export default function AgentsPage() {
  const { agentId: routeAgentId } = useParams();
  const navigate = useNavigate();
  const [agents, setAgents] = useState<AgentDefinition[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const agentId = isAgentId(routeAgentId) ? routeAgentId : null;

  useEffect(() => {
    setIsLoading(true);
    setError(null);
    void api.listAgents()
      .then(setAgents)
      .catch((reason) => setError(errorMessage(reason, "Could not load agents")))
      .finally(() => setIsLoading(false));
  }, []);

  if (routeAgentId && !agentId) return <AgentNotFound />;

  return (
    <section className="page stack agents-page">
      <header className="page-header">
        <div>
          <h1>Agents</h1>
          <p className="muted">Persistent Codex sessions with backend-enforced tool and workspace boundaries.</p>
        </div>
      </header>

      {error && <p className="error-text">{error}</p>}
      {isLoading ? (
        <p className="muted">Loading agents...</p>
      ) : (
        <AgentSelector agents={agents} activeAgentId={agentId} onSelect={(id) => navigate(`/agents/${id}`)} />
      )}

      {agentId && (
        <AgentDetail
          key={agentId}
          agentId={agentId}
          initialAgent={agents.find((agent) => agent.id === agentId) ?? null}
          onAgentUpdate={(updated) => setAgents((current) => current.map((item) => item.id === updated.id ? updated : item))}
        />
      )}
      {!agentId && !isLoading && (
        <section className="placeholder-section">
          <h2>Choose an agent</h2>
          <p className="muted">Open an agent to inspect its effective permissions, manage sessions, and update its policy.</p>
        </section>
      )}
    </section>
  );
}

function AgentSelector({ agents, activeAgentId, onSelect }: {
  agents: AgentDefinition[];
  activeAgentId: AgentId | null;
  onSelect: (agentId: AgentId) => void;
}) {
  return (
    <div className="agent-selector" aria-label="Available agents">
      {agents.map((agent) => (
        <button
          key={agent.id}
          type="button"
          className={`agent-selector-item ${activeAgentId === agent.id ? "active" : ""}`}
          onClick={() => onSelect(agent.id)}
          aria-current={activeAgentId === agent.id ? "page" : undefined}
        >
          <span className={`agent-glyph ${agent.id}`} aria-hidden="true">{agentGlyph(agent.id)}</span>
          <span>
            <strong>{agent.name}</strong>
            <small>{agent.description}</small>
          </span>
          <span className="row-reveal-arrow" aria-hidden="true">→</span>
        </button>
      ))}
    </div>
  );
}

function AgentDetail({ agentId, initialAgent, onAgentUpdate }: {
  agentId: AgentId;
  initialAgent: AgentDefinition | null;
  onAgentUpdate: (agent: AgentDefinition) => void;
}) {
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedSessionId = numberParam(searchParams.get("session"));
  const [agent, setAgent] = useState(initialAgent);
  const [sessions, setSessions] = useState<AgentSessionSummary[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<number | null>(requestedSessionId);
  const [session, setSession] = useState<AgentSession | null>(null);
  const [draft, setDraft] = useState("");
  const [proposals, setProposals] = useState<AssistantProposal[]>([]);
  const [assessment, setAssessment] = useState<AssistantAssessment | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isWorking, setIsWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmAction, setConfirmAction] = useState<ConfirmAction | null>(null);
  const activeTurn = session ? [...session.turns].reverse().find(isActiveTurn) ?? null : null;

  async function loadAgent(options: { showLoading?: boolean } = {}) {
    if (options.showLoading !== false) setIsLoading(true);
    setError(null);
    try {
      const [nextAgent, nextSessions] = await Promise.all([
        api.getAgent(agentId),
        api.listAgentSessions(agentId),
      ]);
      setAgent(nextAgent);
      onAgentUpdate(nextAgent);
      setSessions(nextSessions);
      const nextSessionId = requestedSessionId && nextSessions.some((item) => item.id === requestedSessionId)
        ? requestedSessionId
        : nextSessions[0]?.id ?? null;
      setActiveSessionId((current) => current && nextSessions.some((item) => item.id === current) ? current : nextSessionId);
      if (agentId === "assistant") {
        const [nextProposals, nextAssessment] = await Promise.all([
          api.listAssistantProposals(),
          api.getAssistantAssessment(),
        ]);
        setProposals(nextProposals);
        setAssessment(nextAssessment);
      }
    } catch (reason) {
      setError(errorMessage(reason, `Could not load ${agentId}`));
    } finally {
      if (options.showLoading !== false) setIsLoading(false);
    }
  }

  async function loadSession(sessionId: number) {
    try {
      const nextSession = await api.getAgentSession(agentId, sessionId);
      setSession(nextSession);
      setSessions((current) => mergeSessionSummary(current, nextSession));
    } catch (reason) {
      setError(errorMessage(reason, "Could not load the agent session"));
    }
  }

  useEffect(() => {
    void loadAgent();
    // The full agent surface reloads when the route changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId]);

  useEffect(() => {
    setSession(null);
    if (activeSessionId !== null) {
      setSearchParams({ session: String(activeSessionId) }, { replace: true });
      void loadSession(activeSessionId);
    } else {
      setSearchParams({}, { replace: true });
    }
    // Session loading is keyed by the selected backend session.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId, activeSessionId]);

  usePolling(
    () => activeSessionId === null ? undefined : loadSession(activeSessionId),
    activeTurn !== null,
    1000,
  );

  async function work(action: () => Promise<void>, fallback: string) {
    setIsWorking(true);
    setError(null);
    try {
      await action();
    } catch (reason) {
      setError(errorMessage(reason, fallback));
    } finally {
      setIsWorking(false);
    }
  }

  async function createSession() {
    await work(async () => {
      const created = await api.createAgentSession(agentId);
      setSessions((current) => mergeSessionSummary(current, created));
      setActiveSessionId(created.id);
      setSession(created);
    }, `Could not create a ${agent?.name ?? agentId} session`);
  }

  async function sendTurn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const message = draft.trim();
    if (!message || activeSessionId === null) return;
    await work(async () => {
      setDraft("");
      await api.runAgentTurn(agentId, activeSessionId, message);
      await loadSession(activeSessionId);
    }, "Could not send the message");
  }

  async function confirm() {
    if (!confirmAction) return;
    const action = confirmAction;
    setConfirmAction(null);
    if (action.kind === "policy") {
      await work(async () => {
        const updated = await api.updateAgentPolicy(agentId, action.policy);
        setAgent(updated);
        onAgentUpdate(updated);
      }, "Could not update the agent policy");
      return;
    }
    if (action.kind === "archive-session") {
      await work(async () => {
        await api.archiveAgentSession(agentId, action.sessionId);
        const remaining = sessions.filter((item) => item.id !== action.sessionId);
        setSessions(remaining);
        setActiveSessionId(remaining[0]?.id ?? null);
        setSession(null);
      }, "Could not archive the session");
      return;
    }
    await work(async () => {
      await api.approveAssistantProposal(action.proposalId);
      setProposals(await api.listAssistantProposals());
    }, "Could not approve the plan");
  }

  async function denyProposal(proposalId: number) {
    await work(async () => {
      await api.denyAssistantProposal(proposalId);
      setProposals(await api.listAssistantProposals());
    }, "Could not deny the plan");
  }

  async function updateAssessment(enabled: boolean) {
    await work(async () => setAssessment(await api.updateAssistantAssessment(enabled)), "Could not update the assessment service");
  }

  async function runAssessment() {
    await work(async () => {
      setAssessment(await api.runAssistantAssessment());
      await loadAgent({ showLoading: false });
    }, "Could not start an Assistant assessment");
  }

  if (isLoading && !agent) return <p className="muted">Loading agent details...</p>;
  if (!agent) return <p className="error-text">This agent is unavailable.</p>;

  return (
    <>
      <section className="agent-detail-heading">
        <div>
          <span className={`agent-glyph large ${agent.id}`} aria-hidden="true">{agentGlyph(agent.id)}</span>
          <div><h2>{agent.name}</h2><p className="muted">{agent.description}</p></div>
        </div>
        <div className="agent-boundary-summary">
          <span><small>Filesystem</small>{agent.permissions.filesystem}</span>
          <span><small>Web search</small>{agent.permissions.web_search ? "Allowed" : "Blocked"}</span>
        </div>
      </section>

      {error && <p className="error-text">{error}</p>}

      <div className="section-grid agent-policy-grid">
        <AgentPolicyEditor
          key={JSON.stringify(agent.policy)}
          agent={agent}
          isWorking={isWorking}
          onSave={(policy) => setConfirmAction({ kind: "policy", policy })}
        />
        <AgentFunctionList agent={agent} />
      </div>

      {agentId === "assistant" && (
        <AssistantControls
          assessment={assessment}
          proposals={proposals}
          isWorking={isWorking}
          onAssessmentChange={updateAssessment}
          onRunAssessment={runAssessment}
          onApprove={(proposalId) => setConfirmAction({ kind: "approve-proposal", proposalId })}
          onDeny={denyProposal}
        />
      )}

      <AgentSessions
        agent={agent}
        sessions={sessions}
        session={session}
        activeSessionId={activeSessionId}
        draft={draft}
        isWorking={isWorking}
        activeTurnId={activeTurn?.id ?? null}
        onCreate={createSession}
        onSelect={setActiveSessionId}
        onDraftChange={setDraft}
        onSubmit={sendTurn}
        onCancel={() => activeSessionId !== null && activeTurn
          ? work(async () => { await api.cancelAgentTurn(agentId, activeSessionId, activeTurn.id); await loadSession(activeSessionId); }, "Could not cancel the turn")
          : undefined}
        onArchive={(sessionId) => setConfirmAction({ kind: "archive-session", sessionId })}
      />

      {confirmAction && (
        <AgentConfirmation action={confirmAction} agentName={agent.name} isWorking={isWorking} onConfirm={confirm} onCancel={() => setConfirmAction(null)} />
      )}
    </>
  );
}

function AgentPolicyEditor({ agent, isWorking, onSave }: {
  agent: AgentDefinition;
  isWorking: boolean;
  onSave: (policy: AgentPolicy) => void;
}) {
  const [policy, setPolicy] = useState(agent.policy);
  const [allowedText, setAllowedText] = useState(agent.policy.allowed_functions.join(", "));
  const [bannedText, setBannedText] = useState(agent.policy.banned_functions.join(", "));

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onSave({
      ...policy,
      allowed_functions: parseFunctionList(allowedText),
      banned_functions: parseFunctionList(bannedText),
      model: policy.model?.trim() || null,
      reasoning_effort: policy.reasoning_effort?.trim() || null,
    });
  }

  return (
    <form className="form-panel stack" onSubmit={submit}>
      <div><h2>Permission policy</h2><p className="muted">The backend applies this policy to tool discovery and checks it again for every call.</p></div>
      <div className="form-grid">
        <label>Maximum function risk
          <select value={policy.max_risk} onChange={(event) => setPolicy({ ...policy, max_risk: event.target.value as AgentPolicy["max_risk"] })}>
            <option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option>
          </select>
        </label>
        <label className="checkbox-row agent-policy-checkbox">
          <input type="checkbox" checked={policy.read_only} onChange={(event) => setPolicy({ ...policy, read_only: event.target.checked })} />
          Restrict functions to read-only operations by default
        </label>
        <label>Explicitly allowed function IDs
          <textarea rows={3} value={allowedText} onChange={(event) => setAllowedText(event.target.value)} placeholder="function.one, function.two" />
        </label>
        <label>Explicitly banned function IDs
          <textarea rows={3} value={bannedText} onChange={(event) => setBannedText(event.target.value)} placeholder="function.three" />
        </label>
        <label>Model override
          <input value={policy.model ?? ""} onChange={(event) => setPolicy({ ...policy, model: event.target.value || null })} placeholder="Use backend default" />
        </label>
        <label>Reasoning effort override
          <input value={policy.reasoning_effort ?? ""} onChange={(event) => setPolicy({ ...policy, reasoning_effort: event.target.value || null })} placeholder="Use model default" />
        </label>
      </div>
      <p className="muted">Bans take precedence. Explicit allows can override the risk and read-only defaults, but cannot bypass availability or runtime approval.</p>
      <div className="button-row"><button type="submit" disabled={isWorking}>Review policy change</button></div>
    </form>
  );
}

function AgentFunctionList({ agent }: { agent: AgentDefinition }) {
  return (
    <section className="detail-panel stack">
      <div><h2>Effective functions</h2><p className="muted">Current visibility after policy, availability, and private-tool rules.</p></div>
      <div className="agent-function-list">
        {agent.functions.map((item) => (
          <article key={item.id} className="agent-function-row">
            <div><strong>{item.title}</strong><code>{item.id}</code></div>
            <span className={`badge status-${item.allowed ? "active" : "paused"}`}>{item.allowed ? "allowed" : "blocked"}</span>
            <p>{item.description}</p>
            <small>{item.risk_level} risk · {item.mcp_read_only ? "read-only" : "may write"} · {item.reason}</small>
          </article>
        ))}
        {!agent.functions.length && <p className="muted">No functions are visible to this agent.</p>}
      </div>
    </section>
  );
}

function AssistantControls({ assessment, proposals, isWorking, onAssessmentChange, onRunAssessment, onApprove, onDeny }: {
  assessment: AssistantAssessment | null;
  proposals: AssistantProposal[];
  isWorking: boolean;
  onAssessmentChange: (enabled: boolean) => Promise<void>;
  onRunAssessment: () => Promise<void>;
  onApprove: (proposalId: number) => void;
  onDeny: (proposalId: number) => Promise<void>;
}) {
  return (
    <div className="section-grid assistant-controls">
      <section className="detail-panel stack">
        <div><h2>Assessment service</h2><p className="muted">Reviews current todos and goals every 72 hours. Each occurrence uses a fresh Assistant session.</p></div>
        <dl className="detail-grid">
          <div><dt>Status</dt><dd>{assessment?.enabled ? "Enabled" : "Disabled"}</dd></div>
          <div><dt>Next assessment</dt><dd>{formatTimestamp(assessment?.next_run_at)}</dd></div>
          <div><dt>Last assessment</dt><dd>{formatTimestamp(assessment?.last_run_at)}</dd></div>
          <div><dt>Last status</dt><dd>{assessment?.last_status ?? "Never run"}</dd></div>
        </dl>
        <div className="button-row">
          <button type="button" onClick={() => onAssessmentChange(!assessment?.enabled)} disabled={isWorking || !assessment}>
            {assessment?.enabled ? "Disable" : "Enable"}
          </button>
          <button type="button" className="secondary" onClick={onRunAssessment} disabled={isWorking}>Run Now</button>
        </div>
      </section>
      <section className="detail-panel stack">
        <div><h2>Proposed plans</h2><p className="muted">Approval creates exactly one linked Act session with the instruction shown here.</p></div>
        <div className="assistant-proposal-list">
          {proposals.map((proposal) => (
            <article key={proposal.id} className="assistant-proposal">
              <header><div><strong>{proposal.title}</strong><small>{formatTimestamp(proposal.created_at)}</small></div><span className={`badge status-${proposal.status}`}>{proposal.status}</span></header>
              <p>{proposal.rationale}</p>
              {proposal.actions && <p><strong>Actions:</strong> {proposal.actions}</p>}
              {proposal.references.length > 0 && <p className="muted">References: {proposal.references.join(", ")}</p>}
              <details><summary>Act instruction</summary><pre>{proposal.instruction}</pre></details>
              {proposal.act_session_id !== null && <Link to={`/agents/act?session=${proposal.act_session_id}`}>Open linked Act session</Link>}
              {proposal.execution_status && <p className="muted">Execution: {proposal.execution_status}</p>}
              {proposal.status === "pending" && <div className="button-row"><button type="button" onClick={() => onApprove(proposal.id)} disabled={isWorking}>Approve plan</button><button type="button" className="secondary" onClick={() => onDeny(proposal.id)} disabled={isWorking}>Deny</button></div>}
            </article>
          ))}
          {!proposals.length && <p className="muted">No plans have been proposed.</p>}
        </div>
      </section>
    </div>
  );
}

function AgentSessions({ agent, sessions, session, activeSessionId, draft, isWorking, activeTurnId, onCreate, onSelect, onDraftChange, onSubmit, onCancel, onArchive }: {
  agent: AgentDefinition;
  sessions: AgentSessionSummary[];
  session: AgentSession | null;
  activeSessionId: number | null;
  draft: string;
  isWorking: boolean;
  activeTurnId: number | null;
  onCreate: () => Promise<void>;
  onSelect: (sessionId: number) => void;
  onDraftChange: (value: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => Promise<void>;
  onCancel: () => void;
  onArchive: (sessionId: number) => void;
}) {
  return (
    <section className="agent-sessions-section stack">
      <div className="section-heading"><div><h2>Sessions</h2><p className="muted">Long-lived conversations backed by Codex App Server.</p></div><button type="button" onClick={onCreate} disabled={isWorking}>New {agent.name} session</button></div>
      {agent.id === "assistant" && <p className="agent-retention-note">Eidolon keeps at most the five most recently created Assistant sessions. Proposals and linked Act executions remain available after session cleanup.</p>}
      <div className="agent-session-workspace">
        <aside className="agent-session-list" aria-label={`${agent.name} sessions`}>
          {sessions.map((item) => <button key={item.id} type="button" className={item.id === activeSessionId ? "active" : ""} onClick={() => onSelect(item.id)}><strong>{item.title}</strong><small>{formatTimestamp(item.created_at)}</small></button>)}
          {!sessions.length && <p className="muted">No sessions yet.</p>}
        </aside>
        <div className="agent-conversation">
          {session ? (
            <>
              <div className="chat-mode-bar"><span><strong>{session.title}</strong><small>{session.status}</small></span><button type="button" className="secondary" onClick={() => onArchive(session.id)} disabled={isWorking || activeTurnId !== null}>Archive session</button></div>
              <div className="message-list" aria-live="polite">
                {session.turns.map((turn) => (
                  <div key={turn.id} className="agent-turn">
                    <article className="message user"><span>You</span><p>{turn.user_message}</p></article>
                    <article className={`message assistant ${isActiveTurn(turn) ? "thinking-message" : ""}`}><span>{agent.name}</span><div className="message-body">{isActiveTurn(turn) && <div className="thinking-row"><span className="thinking-spinner" aria-hidden="true" /><p>{agent.name} is thinking...</p></div>}{turn.assistant_message && <p>{turn.assistant_message}</p>}{turn.error_message && <p className="error-text">{turn.error_message}</p>}{turn.activity_json.length > 0 && <details className="act-work-details"><summary>{turn.activity_json.length} recorded activit{turn.activity_json.length === 1 ? "y" : "ies"}</summary><div className="act-work-details-content"><ul>{turn.activity_json.map((activity, index) => <li key={`${activity.kind}-${index}`}>{activity.label}</li>)}</ul></div></details>}</div></article>
                  </div>
                ))}
                {!session.turns.length && <div className="agent-empty-conversation"><p>Start a conversation with {agent.name}.</p></div>}
              </div>
              <form className="composer" onSubmit={onSubmit}>
                <input value={draft} onChange={(event) => onDraftChange(event.target.value)} placeholder={`Message ${agent.name}`} aria-label={`Message ${agent.name}`} disabled={isWorking || activeTurnId !== null} />
                <button type="submit" className="chat-send-button" disabled={isWorking || activeTurnId !== null || !draft.trim()} aria-label="Send message"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 7 5 11l4 4M5 11h8a4 4 0 0 0 4-4V5" /></svg></button>
                {activeTurnId !== null && <button type="button" className="secondary" onClick={onCancel} disabled={isWorking}>Cancel</button>}
              </form>
            </>
          ) : <div className="agent-empty-conversation"><p>Select or create a session.</p></div>}
        </div>
      </div>
    </section>
  );
}

type ConfirmAction =
  | { kind: "policy"; policy: AgentPolicy }
  | { kind: "archive-session"; sessionId: number }
  | { kind: "approve-proposal"; proposalId: number };

function AgentConfirmation({ action, agentName, isWorking, onConfirm, onCancel }: { action: ConfirmAction; agentName: string; isWorking: boolean; onConfirm: () => Promise<void>; onCancel: () => void }) {
  const copy = action.kind === "policy"
    ? { title: `Update ${agentName} policy?`, body: "This changes which tools the agent can discover and call on subsequent turns.", confirm: "Update policy" }
    : action.kind === "archive-session"
      ? { title: "Archive agent session?", body: "The session will leave the active list, while its stored conversation history remains available to Eidolon. Assistant proposals remain available.", confirm: "Archive session" }
      : { title: "Approve this plan?", body: "Approval creates one new Act session and sends it the plan's exact instruction. Act's normal permission checks still apply.", confirm: "Approve and start Act" };
  return <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="agent-confirmation-title"><section className="modal-panel permission-modal"><div><h2 id="agent-confirmation-title">{copy.title}</h2><p className="muted">{copy.body}</p></div><div className="button-row"><button type="button" onClick={onConfirm} disabled={isWorking}>{copy.confirm}</button><button type="button" className="secondary" onClick={onCancel} disabled={isWorking}>Cancel</button></div></section></div>;
}

function AgentNotFound() {
  return <section className="page stack"><header className="page-header"><div><h1>Agent not found</h1><p className="muted">This agent ID is not part of Eidolon’s configured agent set.</p></div></header><Link to="/agents">Return to Agents</Link></section>;
}

function isAgentId(value: string | undefined): value is AgentId { return AGENT_IDS.includes(value as AgentId); }
function isActiveTurn(turn: { status: string }) { return turn.status === "queued" || turn.status === "running"; }
function parseFunctionList(value: string) { return [...new Set(value.split(/[\n,]/).map((item) => item.trim()).filter(Boolean))]; }
function numberParam(value: string | null) { const parsed = Number(value); return value && Number.isInteger(parsed) && parsed > 0 ? parsed : null; }
function agentGlyph(agentId: AgentId) { return agentId === "act" ? "↯" : agentId === "observer" ? "◉" : "✦"; }
function errorMessage(reason: unknown, fallback: string) { return reason instanceof Error ? reason.message : fallback; }
function formatTimestamp(value: string | null | undefined) { if (!value) return "Not scheduled"; const date = parseBackendDateTime(value); return Number.isNaN(date.getTime()) ? "Unknown" : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date); }
function mergeSessionSummary(sessions: AgentSessionSummary[], session: AgentSession): AgentSessionSummary[] { const { turns: _turns, ...summary } = session; return [summary, ...sessions.filter((item) => item.id !== session.id)].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at)); }
