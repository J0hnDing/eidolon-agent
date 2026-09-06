import { FormEvent, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import {
  AgentRun,
  ApprovalRequest,
  ConversationMode,
  ProposedSkillValidation,
} from "../../api/client";
import { DeleteIconButton } from "../../components/DeleteIconButton";
import { ChatConversation, ChatMessage } from "../../lib/chatStore";

type ChatWorkspaceProps = {
  conversations: ChatConversation[];
  activeConversationId: string;
  messages: ChatMessage[];
  draft: string;
  mode: ConversationMode;
  isBusy: boolean;
  isSending: boolean;
  isGenerating: boolean;
  error: string | null;
  onNewConversation: (mode: ConversationMode) => void;
  onSelectConversation: (conversationId: string) => void;
  onDeleteConversation: (conversationId: string) => void;
  activeActTurnId: number | null;
  onCancelAct: () => void;
  onDraftChange: (draft: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onApproveBuild: (message: ChatMessage) => void;
  onDenyBuild: (message: ChatMessage) => void;
  onApproveRuntime: (message: ChatMessage) => void;
  onDenyRuntime: (message: ChatMessage) => void;
};

export function ChatWorkspace({
  conversations,
  activeConversationId,
  messages,
  draft,
  mode,
  isBusy,
  isSending,
  isGenerating,
  error,
  onNewConversation,
  onSelectConversation,
  onDeleteConversation,
  activeActTurnId,
  onCancelAct,
  onDraftChange,
  onSubmit,
  onApproveBuild,
  onDenyBuild,
  onApproveRuntime,
  onDenyRuntime,
}: ChatWorkspaceProps) {
  return (
    <section className="page stack">
      <header className="page-header">
        <div>
          <h1>Chat</h1>
        </div>
      </header>

      <div className="chat-workspace">
        <ChatSidebar
          conversations={conversations}
          activeConversationId={activeConversationId}
          onNewConversation={onNewConversation}
          onSelectConversation={onSelectConversation}
        />
        <div className="chat-panel">
          <ConversationModeBar
            mode={mode}
            activeConversationId={activeConversationId}
            isBusy={isBusy}
            onDeleteConversation={onDeleteConversation}
          />
          <MessageList
            key={activeConversationId}
            messages={messages}
            isSending={isSending}
            isGenerating={isGenerating}
            onApproveBuild={onApproveBuild}
            onDenyBuild={onDenyBuild}
            onApproveRuntime={onApproveRuntime}
            onDenyRuntime={onDenyRuntime}
          />
          <ChatComposer
            draft={draft}
            mode={mode}
            isBusy={isBusy}
            isSending={isSending}
            onDraftChange={onDraftChange}
            onSubmit={onSubmit}
            activeActTurnId={activeActTurnId}
            onCancelAct={onCancelAct}
          />
        </div>
      </div>
      {error && <p className="error-text">{error}</p>}
    </section>
  );
}

function ChatSidebar({
  conversations,
  activeConversationId,
  onNewConversation,
  onSelectConversation,
}: Pick<
  ChatWorkspaceProps,
  | "conversations"
  | "activeConversationId"
  | "onNewConversation"
  | "onSelectConversation"
>) {
  const [isCreatePopoverOpen, setIsCreatePopoverOpen] = useState(false);
  const createButtonRef = useRef<HTMLButtonElement>(null);
  const firstCreateOptionRef = useRef<HTMLButtonElement>(null);
  const createPopoverRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!isCreatePopoverOpen) return undefined;
    firstCreateOptionRef.current?.focus();
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (createPopoverRef.current?.contains(target) || createButtonRef.current?.contains(target)) return;
      setIsCreatePopoverOpen(false);
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setIsCreatePopoverOpen(false);
        createButtonRef.current?.focus();
        return;
      }
      if (event.key !== "Tab") return;
      const popover = createPopoverRef.current;
      if (!popover) return;
      const focusable = Array.from(popover.querySelectorAll<HTMLElement>(
        "button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex='-1'])",
      ));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!popover.contains(document.activeElement)) {
        event.preventDefault();
        first.focus();
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("pointerdown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("pointerdown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [isCreatePopoverOpen]);

  function closeCreatePopover() {
    setIsCreatePopoverOpen(false);
    createButtonRef.current?.focus();
  }

  return (
    <aside className="chat-sidebar" aria-label="Chats">
      <div className="chat-thread-list">
        {conversations.map((conversation) => (
          <div
            key={conversation.id}
            className={`chat-thread-row ${conversation.id === activeConversationId ? "active" : ""}`}
          >
            <button
              type="button"
              className="chat-thread-select"
              onClick={() => onSelectConversation(conversation.id)}
            >
              <span className="chat-thread-heading">
                <span className={`chat-thread-title ${conversation.mode}`}>
                  <span className="chat-thread-icon" aria-hidden="true">{modeSymbol(conversation.mode)}</span>
                  <strong>{conversation.title}</strong>
                </span>
                <ConversationModeMarker mode={conversation.mode} showSymbol={false} />
              </span>
            </button>
          </div>
        ))}
      </div>
      <div className="chat-create-actions" aria-label="Create conversation">
        <button
          ref={createButtonRef}
          type="button"
          aria-haspopup="dialog"
          aria-expanded={isCreatePopoverOpen}
          aria-controls="chat-create-popover"
          onClick={() => setIsCreatePopoverOpen((open) => !open)}
        >
          <span className="conversation-mode-symbol" aria-hidden="true">＋</span>
          <span>New Chat</span>
        </button>
        {isCreatePopoverOpen && (
          <div className="chat-create-popover-layer">
            <aside
              id="chat-create-popover"
              className="chat-create-popover"
              ref={createPopoverRef}
              role="dialog"
              aria-modal="true"
              aria-labelledby="chat-create-popover-title"
            >
              <header className="chat-create-popover-header">
                <div>
                  <h2 id="chat-create-popover-title">New Chat</h2>
                  <p className="muted">Choose a conversation mode.</p>
                </div>
                <button type="button" className="square-icon-button secondary" onClick={closeCreatePopover} aria-label="Close new chat menu">
                  ×
                </button>
              </header>
              <div className="chat-create-options">
                {conversationModeOptions.map((option, index) => (
                  <button
                    key={option.mode}
                    ref={index === 0 ? firstCreateOptionRef : undefined}
                    type="button"
                    className={`chat-create-option ${option.mode}`}
                    aria-label={`Create ${option.label}`}
                    onClick={() => {
                      onNewConversation(option.mode);
                      closeCreatePopover();
                    }}
                  >
                    <span className="conversation-mode-symbol" aria-hidden="true">{modeSymbol(option.mode)}</span>
                    <span>
                      <strong>Create {option.label}</strong>
                      <small>{option.description}</small>
                    </span>
                  </button>
                ))}
              </div>
            </aside>
          </div>
        )}
      </div>
    </aside>
  );
}

function ConversationModeBar({
  mode,
  activeConversationId,
  isBusy,
  onDeleteConversation,
}: Pick<ChatWorkspaceProps, "mode" | "activeConversationId" | "isBusy" | "onDeleteConversation">) {
  return (
    <div className="chat-mode-bar">
      <ConversationModeMarker mode={mode} />
      <span className="muted">
        {modeDescription(mode)}
      </span>
      <DeleteIconButton
        onClick={() => onDeleteConversation(activeConversationId)}
        disabled={isBusy}
        label="Delete conversation"
      />
    </div>
  );
}

function MessageList({
  messages,
  isSending,
  isGenerating,
  onApproveBuild,
  onDenyBuild,
  onApproveRuntime,
  onDenyRuntime,
}: Pick<
  ChatWorkspaceProps,
  | "messages"
  | "isSending"
  | "isGenerating"
  | "onApproveBuild"
  | "onDenyBuild"
  | "onApproveRuntime"
  | "onDenyRuntime"
>) {
  return (
    <div className="message-list" aria-live="polite">
      {messages.map((message) => (
        <article key={message.id} className={`message ${message.role}`}>
          <span>{message.role === "assistant" ? "Eidolon" : "You"}</span>
          <MessageBody
            message={message}
            isWorking={isGenerating}
            onApproveBuild={onApproveBuild}
            onDenyBuild={onDenyBuild}
            onApproveRuntime={onApproveRuntime}
            onDenyRuntime={onDenyRuntime}
          />
        </article>
      ))}
      {(isSending || isGenerating) && !messages.some((message) => message.kind === "thinking") && (
        <article className="message assistant thinking-message" aria-label="Eidolon is thinking">
          <span>Eidolon</span>
          <div className="thinking-row">
            <span className="thinking-spinner" aria-hidden="true" />
            <p>{isGenerating ? "Eidolon is generating..." : "Eidolon is thinking..."}</p>
          </div>
        </article>
      )}
    </div>
  );
}

function ChatComposer({
  draft,
  mode,
  isBusy,
  isSending,
  onDraftChange,
  onSubmit,
  activeActTurnId,
  onCancelAct,
}: Pick<
  ChatWorkspaceProps,
  "draft" | "mode" | "isBusy" | "isSending" | "onDraftChange" | "onSubmit" | "activeActTurnId" | "onCancelAct"
>) {
  return (
    <form className="composer" onSubmit={onSubmit}>
      <input
        value={draft}
        onChange={(event) => onDraftChange(event.target.value)}
        placeholder={
          isBusy
            ? mode === "project" ? "Project build is running..." : `${modeLabel(mode)} is thinking...`
            : mode === "project"
              ? "Describe the skill you want to propose"
              : `Ask ${modeLabel(mode)} ${mode === "act" ? "to work in its workspace" : mode === "observer" ? "to inspect local context" : "to assess goals and propose work"}`
        }
        aria-label="Conversation message"
        disabled={isBusy && mode === "project"}
      />
      <button
        type="submit"
        className="chat-send-button"
        disabled={isBusy}
        aria-label={isSending ? "Sending message" : "Send message"}
        title={isSending ? "Sending" : "Send message"}
      >
        {isSending ? (
          <span className="thinking-spinner" aria-hidden="true" />
        ) : (
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M9 7 5 11l4 4M5 11h8a4 4 0 0 0 4-4V5" />
          </svg>
        )}
      </button>
      {mode !== "project" && activeActTurnId !== null && (
        <button type="button" className="secondary" onClick={onCancelAct}>Cancel</button>
      )}
    </form>
  );
}

function modeLabel(mode: ConversationMode): string {
  return conversationModeOptions.find((option) => option.mode === mode)?.label ?? mode;
}

function modeSymbol(mode: ConversationMode): string {
  return mode === "project" ? "◇" : mode === "act" ? "↯" : mode === "observer" ? "◉" : "✦";
}

function modeDescription(mode: ConversationMode): string {
  return conversationModeOptions.find((option) => option.mode === mode)?.description ?? "";
}

function ConversationModeMarker({ mode, showSymbol = true }: { mode: ConversationMode; showSymbol?: boolean }) {
  return (
    <span className={`conversation-mode-marker ${mode}`}>
      {showSymbol && <span className="conversation-mode-symbol" aria-hidden="true">{modeSymbol(mode)}</span>}
      <span>{modeLabel(mode)}</span>
    </span>
  );
}

const conversationModeOptions: Array<{ mode: ConversationMode; label: string; description: string }> = [
  { mode: "project", label: "Project", description: "Creates proposed skills after approval." },
  { mode: "act", label: "Act", description: "Works persistently in the shared workspace with Eidolon tools." },
  { mode: "observer", label: "Observer", description: "Reads local context without making changes." },
  { mode: "assistant", label: "Assistant", description: "Assesses goals and proposes work for Act." },
];

function MessageBody({
  message,
  isWorking,
  onApproveBuild,
  onDenyBuild,
  onApproveRuntime,
  onDenyRuntime,
}: {
  message: ChatMessage;
  isWorking: boolean;
  onApproveBuild: (message: ChatMessage) => void;
  onDenyBuild: (message: ChatMessage) => void;
  onApproveRuntime: (message: ChatMessage) => void;
  onDenyRuntime: (message: ChatMessage) => void;
}) {
  if (message.kind === "thinking") {
    return (
      <div className="message-body">
        <div className="thinking-row">
          <span className="thinking-spinner" aria-hidden="true" />
          <p>{message.content}</p>
        </div>
        {message.actWork && <ActWorkDetails work={message.actWork} />}
      </div>
    );
  }
  if (message.kind === "build_approval" && message.permissionRequest) {
    return (
      <div className="chat-approval-card">
        <p>{message.content}</p>
        <PermissionSummary request={message.permissionRequest} />
        <ApprovalButtons
          status={message.actionStatus ?? "pending"}
          isWorking={isWorking}
          approveLabel="Approve Generation"
          denyLabel="Decline"
          approveDisabled={false}
          onApprove={() => onApproveBuild(message)}
          onDeny={() => onDenyBuild(message)}
        />
      </div>
    );
  }
  if (message.kind === "runtime_approval" && message.permissionRequest) {
    return (
      <div className="chat-approval-card">
        <p>{message.content}</p>
        {message.skill && <p><Link to={`/skills/${message.skill.id}`}>Open {message.skill.name}</Link></p>}
        <PermissionSummary request={message.permissionRequest} />
        <ApprovalButtons
          status={message.actionStatus ?? "pending"}
          isWorking={isWorking}
          approveLabel="Approve Runtime Permissions"
          denyLabel="Deny"
          approveDisabled={message.permissionRequest.risk_level === "blocked"}
          onApprove={() => onApproveRuntime(message)}
          onDeny={() => onDenyRuntime(message)}
        />
      </div>
    );
  }
  if (message.kind === "generation_result") {
    return (
      <div className="chat-approval-card">
        <p>{message.content}</p>
        {message.skill && <p><Link to={`/skills/${message.skill.id}`}>Open {message.skill.name}</Link></p>}
        {message.agentRun && <p><Link to={`/agent-runs/${message.agentRun.id}`}>Open Agent Run #{message.agentRun.id}</Link></p>}
        {message.validation && <p className="muted">Validation {message.validation.ok ? "passed" : "needs attention"}.</p>}
      </div>
    );
  }
  return (
    <div className="message-body">
      <p>{message.content}</p>
      {message.actWork && <ActWorkDetails work={message.actWork} />}
    </div>
  );
}

function ActWorkDetails({ work }: { work: NonNullable<ChatMessage["actWork"]> }) {
  const active = work.status === "queued" || work.status === "running";
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!active) return undefined;
    const interval = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(interval);
  }, [active]);

  const startedAt = Date.parse(work.startedAt);
  const completedAt = work.completedAt ? Date.parse(work.completedAt) : now;
  const elapsedMilliseconds = Number.isFinite(startedAt) && Number.isFinite(completedAt)
    ? Math.max(0, completedAt - startedAt)
    : 0;
  const toolCount = work.activities.filter((activity) => activity.kind === "mcpToolCall").length;
  const visibleActivities = work.activities.filter((activity) => activity.kind !== "started");
  const summary = `${active ? "Working" : "Worked"} for ${formatElapsedTime(elapsedMilliseconds)}`
    + (toolCount ? ` · ${toolCount} tool${toolCount === 1 ? "" : "s"} used` : "");

  return (
    <details className="act-work-details">
      <summary>{summary}</summary>
      <div className="act-work-details-content">
        {visibleActivities.length ? (
          <ul>
            {visibleActivities.map((activity, index) => (
              <li key={`${activity.kind}-${index}`}>{activity.label}</li>
            ))}
          </ul>
        ) : (
          <p className="muted">No tool activity recorded{active ? " yet" : ""}.</p>
        )}
      </div>
    </details>
  );
}

function formatElapsedTime(milliseconds: number): string {
  const totalSeconds = Math.max(0, Math.round(milliseconds / 1000));
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return seconds ? `${minutes}m ${seconds}s` : `${minutes}m`;
}

function ApprovalButtons({
  status,
  isWorking,
  approveLabel,
  denyLabel,
  approveDisabled,
  onApprove,
  onDeny,
}: {
  status: ChatMessage["actionStatus"];
  isWorking: boolean;
  approveLabel: string;
  denyLabel: string;
  approveDisabled: boolean;
  onApprove: () => void;
  onDeny: () => void;
}) {
  if (status && !["pending", "working", "failed"].includes(status)) {
    return <p className="muted">Status: {status}</p>;
  }
  return (
    <div className="button-row">
      <button type="button" onClick={onApprove} disabled={isWorking || status === "working" || approveDisabled}>
        {status === "working" ? "Working..." : approveLabel}
      </button>
      <button type="button" className="secondary" onClick={onDeny} disabled={isWorking || status === "working"}>
        {denyLabel}
      </button>
    </div>
  );
}

function PermissionSummary({ request }: { request: ApprovalRequest }) {
  const pmSummary = getReasonText(request, "product_manager_summary");
  const permissionSummary = getReasonText(request, "permission_review_summary") || getReasonText(request, "security_reviewer_summary");
  const expansion = request.reason_json.permission_expansion;
  return (
    <div className="permission-inline-summary">
      {pmSummary && <section><h3>ProductManager Summary</h3><p>{pmSummary}</p></section>}
      {permissionSummary && <section><h3>Permission Review</h3><p>{permissionSummary}</p></section>}
      <section>
        <h3>Requested Access</h3>
        <div className="chip-list">
          {summarizePermissionLabels(request).map((label) => <span key={label} className="permission-chip">{label}</span>)}
        </div>
      </section>
      {isNonEmptyObject(expansion) && <p className="error-text">Permission expansion detected: {JSON.stringify(expansion)}</p>}
      <p className="muted">
        Approval lets the next controlled step continue. It does not install the skill, run the skill, or grant automatic execution.
      </p>
    </div>
  );
}

export function buildApprovalMessage(displayName: string): string {
  return `I prepared a skill blueprint for ${displayName}. Review the ProductManager summary and permission review below, then approve or decline generation.`;
}

export function generationResultMessage(
  skillName: string,
  agentRun: AgentRun | null,
  validation: ProposedSkillValidation | null,
): string {
  const summary = getAgentRunSummary(agentRun);
  const validationText = validation
    ? `Validation ${validation.ok ? "passed" : "needs attention"}.`
    : "Validation status was not returned.";
  return [
    summary || `Generated proposed skill ${skillName}.`,
    validationText,
    "The skill has not been installed or run.",
    "Runtime permissions are reviewed separately below.",
  ].join("\n\n");
}

export function runtimeApprovalMessage(skillName: string, request: ApprovalRequest): string {
  const expansionText = isNonEmptyObject(request.reason_json.permission_expansion)
    ? " Permission expansion was detected and must be reviewed."
    : "";
  return `The generated skill ${skillName} is ready for runtime permission review.${expansionText}`;
}

function getAgentRunSummary(agentRun: AgentRun | null): string | null {
  if (!agentRun) return null;
  const finalSummary = agentRun.final_summary_json;
  if (finalSummary && typeof finalSummary.user_summary === "string" && finalSummary.user_summary.trim()) {
    return finalSummary.user_summary;
  }
  return agentRun.summary || null;
}

function getReasonText(request: ApprovalRequest, key: string): string | null {
  const value = request.reason_json[key];
  return typeof value === "string" && value.trim() ? value : null;
}

function summarizePermissionLabels(request: ApprovalRequest): string[] {
  const labels: string[] = [];
  if (request.requested_network_domains_json.length) labels.push(`Network: ${request.requested_network_domains_json.join(", ")}`);
  if (request.requested_dependencies_json.length) labels.push(`Packages: ${request.requested_dependencies_json.join(", ")}`);
  const filesystem = request.requested_filesystem_json;
  if (Array.isArray(filesystem.filesystem_read) && filesystem.filesystem_read.length) labels.push(`Read: ${filesystem.filesystem_read.join(", ")}`);
  if (Array.isArray(filesystem.filesystem_write) && filesystem.filesystem_write.length) labels.push(`Write: ${filesystem.filesystem_write.join(", ")}`);
  const permissions = request.requested_permissions_json;
  if (permissions.codex_generation) labels.push("Codex generation");
  if (permissions.internet_research) labels.push("Internet research");
  return labels.length ? labels : ["No special permissions"];
}

function isNonEmptyObject(value: unknown): boolean {
  return typeof value === "object" && value !== null && !Array.isArray(value) && Object.keys(value).length > 0;
}
