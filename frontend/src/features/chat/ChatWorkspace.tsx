import { FormEvent } from "react";
import { Link } from "react-router-dom";

import {
  AgentRun,
  ApprovalRequest,
  ChatMode,
  ProposedSkillValidation,
} from "../../api/client";
import { ChatConversation, ChatMessage } from "../../lib/chatStore";
import { formatSystemDateTime } from "../../lib/dateTime";

type ChatWorkspaceProps = {
  conversations: ChatConversation[];
  activeConversationId: string;
  messages: ChatMessage[];
  draft: string;
  mode: ChatMode;
  isBusy: boolean;
  isSending: boolean;
  isGenerating: boolean;
  error: string | null;
  onNewConversation: (mode: ChatMode) => void;
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
          <p className="eyebrow">Conversation</p>
          <h1>Chat</h1>
        </div>
      </header>

      <div className="chat-workspace">
        <ChatSidebar
          conversations={conversations}
          activeConversationId={activeConversationId}
          isBusy={isBusy}
          onNewConversation={onNewConversation}
          onSelectConversation={onSelectConversation}
          onDeleteConversation={onDeleteConversation}
        />
        <div className="chat-panel">
          <ChatModeBar mode={mode} />
          <MessageList
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
  isBusy,
  onNewConversation,
  onSelectConversation,
  onDeleteConversation,
}: Pick<
  ChatWorkspaceProps,
  | "conversations"
  | "activeConversationId"
  | "isBusy"
  | "onNewConversation"
  | "onSelectConversation"
  | "onDeleteConversation"
>) {
  return (
    <aside className="chat-sidebar" aria-label="Chats">
      <div className="chat-create-actions" aria-label="Create conversation">
        {(["chat", "project", "act"] as ChatMode[]).map((mode) => (
          <button key={mode} type="button" onClick={() => onNewConversation(mode)}>
            New {mode === "chat" ? "Chat" : mode === "project" ? "Project" : "Act"}
          </button>
        ))}
      </div>
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
                <strong>{conversation.title}</strong>
                <small className={`conversation-mode-badge ${conversation.mode}`}>{modeLabel(conversation.mode)}</small>
              </span>
              <span>{formatSystemDateTime(conversation.updatedAt)}</span>
            </button>
            <button
              type="button"
              className="chat-thread-delete"
              onClick={() => onDeleteConversation(conversation.id)}
              disabled={isBusy}
              aria-label={`Delete ${conversation.mode} conversation ${conversation.title}`}
              title={`Delete ${modeLabel(conversation.mode)} conversation`}
            >
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M5 7h14M9 7V4h6v3m2 0-1 13H8L7 7m3 4v5m4-5v5" />
              </svg>
            </button>
          </div>
        ))}
      </div>
    </aside>
  );
}

function ChatModeBar({ mode }: Pick<ChatWorkspaceProps, "mode">) {
  return (
    <div className="chat-mode-bar">
      <span className="muted">
        {mode === "project"
          ? "Project mode creates proposed skills after approval."
          : mode === "act"
            ? "Act works persistently in the shared workspace with Eidolon tools."
            : "Chat mode asks Codex for normal answers and will not build skills."}
      </span>
      <span className={`conversation-mode-badge ${mode}`}>{modeLabel(mode)}</span>
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
          <span>{message.role}</span>
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
        <article className="message assistant thinking-message" aria-label="Codex is thinking">
          <span>assistant</span>
          <div className="thinking-row">
            <span className="thinking-spinner" aria-hidden="true" />
            <p>{isGenerating ? "Codex is generating..." : "Codex is thinking..."}</p>
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
            ? "Project build is running..."
            : mode === "project"
              ? "Describe the skill you want to propose"
              : mode === "act"
                ? "Ask Act to work in its workspace"
                : "Type a message"
        }
        aria-label="Chat message"
        disabled={isBusy && mode === "project"}
      />
      <button type="submit" disabled={isBusy}>{isSending ? "Sending..." : "Send"}</button>
      {mode === "act" && activeActTurnId !== null && (
        <button type="button" className="secondary" onClick={onCancelAct}>Cancel</button>
      )}
    </form>
  );
}

function modeLabel(mode: ChatMode): string {
  return mode === "chat" ? "Chat" : mode === "project" ? "Project" : "Act";
}

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
  return <p>{message.content}</p>;
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
