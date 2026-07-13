import { FormEvent, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import {
  AgentRun,
  ApprovalRequest,
  ChatMode,
  ProposedSkillValidation,
  Skill,
  SkillGenerationApprovalResponse,
  SkillGenerationRequest,
  api,
} from "../api/client";
import {
  appendMessages as appendStoredMessages,
  CHAT_UPDATED_EVENT,
  ChatConversation,
  ChatMessage,
  createConversation,
  initialMessages,
  loadActiveConversationId,
  loadStoredConversations,
  saveActiveConversationId,
  saveStoredConversations,
  updateStoredConversations,
} from "../lib/chatStore";

export default function ChatPage() {
  const [conversations, setConversations] = useState<ChatConversation[]>(() => loadStoredConversations());
  const [activeConversationId, setActiveConversationId] = useState(() => loadActiveConversationId(conversations));
  const [approvalResult, setApprovalResult] = useState<SkillGenerationApprovalResponse | null>(null);
  const [isSending, setIsSending] = useState(false);
  const [isGenerating, setIsGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const activeConversation = useMemo(
    () => conversations.find((conversation) => conversation.id === activeConversationId) ?? conversations[0],
    [activeConversationId, conversations],
  );
  const messages = activeConversation?.messages ?? initialMessages;
  const draft = activeConversation?.draft ?? "";
  const mode = activeConversation?.mode ?? "chat";
  const hasPendingMessage = messages.some(
    (message) => message.kind === "thinking" || message.actionStatus === "working",
  );
  const isBusy = isSending || isGenerating || hasPendingMessage;

  useEffect(() => {
    if (conversations.length === 0) {
      const conversation = createConversation();
      saveStoredConversations([conversation]);
      setConversations([conversation]);
      selectConversation(conversation.id);
    }
  }, [conversations.length]);

  useEffect(() => {
    function handleStoredChatUpdate() {
      setConversations(loadStoredConversations());
    }
    window.addEventListener(CHAT_UPDATED_EVENT, handleStoredChatUpdate);
    return () => window.removeEventListener(CHAT_UPDATED_EVENT, handleStoredChatUpdate);
  }, []);

  function updateConversation(conversationId: string, updater: (conversation: ChatConversation) => ChatConversation) {
    const nextConversations = updateStoredConversations((current) =>
      current.map((conversation) =>
        conversation.id === conversationId
          ? updater(conversation)
          : conversation,
      ),
    );
    setConversations(nextConversations);
  }

  function updateActiveConversation(updater: (conversation: ChatConversation) => ChatConversation) {
    updateConversation(activeConversationId, updater);
  }

  function appendMessagesToConversation(conversationId: string, newMessages: ChatMessage[]) {
    updateConversation(conversationId, (conversation) => {
      return appendStoredMessages(conversation, newMessages);
    });
  }

  function appendMessages(newMessages: ChatMessage[]) {
    appendMessagesToConversation(activeConversationId, newMessages);
  }

  function handleNewChat() {
    const conversation = createConversation();
    const nextConversations = updateStoredConversations((current) => [conversation, ...current]);
    setConversations(nextConversations);
    selectConversation(conversation.id);
    setError(null);
    setApprovalResult(null);
  }

  function selectConversation(conversationId: string) {
    saveActiveConversationId(conversationId);
    setActiveConversationId(conversationId);
  }

  async function handleDeleteConversation(conversationId: string) {
    const nextConversations = updateStoredConversations((current) => {
      const remaining = current.filter((conversation) => conversation.id !== conversationId);
      return remaining.length ? remaining : [createConversation()];
    });
    setConversations(nextConversations);
    if (conversationId === activeConversationId || !nextConversations.some((conversation) => conversation.id === activeConversationId)) {
      const nextActiveId = nextConversations[0].id;
      saveActiveConversationId(nextActiveId);
      setActiveConversationId(nextActiveId);
      setApprovalResult(null);
    }
    setError(null);

    try {
      await api.deleteChatConversation(conversationId);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Could not clean up backend chat history";
      setError(`Deleted the local chat, but backend history cleanup failed: ${message}`);
    }
  }

  function updateDraft(value: string) {
    updateActiveConversation((conversation) => ({
      ...conversation,
      draft: value,
      updatedAt: new Date().toISOString(),
    }));
  }

  function updateMode(value: ChatMode) {
    updateActiveConversation((conversation) => ({
      ...conversation,
      mode: value,
      updatedAt: new Date().toISOString(),
    }));
  }

  function updateMessageInConversation(
    conversationId: string,
    messageId: number,
    updater: (message: ChatMessage) => ChatMessage,
  ) {
    updateConversation(conversationId, (conversation) => ({
      ...conversation,
      messages: conversation.messages.map((message) => (message.id === messageId ? updater(message) : message)),
      updatedAt: new Date().toISOString(),
    }));
  }

  function updateMessage(messageId: number, updater: (message: ChatMessage) => ChatMessage) {
    updateMessageInConversation(activeConversationId, messageId, updater);
  }

  function removeMessageFromConversation(conversationId: string, messageId: number) {
    updateConversation(conversationId, (conversation) => ({
      ...conversation,
      messages: conversation.messages.filter((message) => message.id !== messageId),
      updatedAt: new Date().toISOString(),
    }));
  }

  function removeMessage(messageId: number) {
    removeMessageFromConversation(activeConversationId, messageId);
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const content = draft.trim();
    if (!content) return;

    const conversationId = activeConversationId;
    const nextId = Date.now();
    const thinkingId = nextId + 1;
    appendMessagesToConversation(conversationId, [
      { id: nextId, role: "user", content },
      {
        id: thinkingId,
        role: "assistant",
        content: mode === "project" ? "ProductManager is reviewing the project..." : "Codex is thinking...",
        kind: "thinking",
      },
    ]);
    updateConversation(conversationId, (conversation) => ({ ...conversation, draft: "" }));
    setIsSending(true);
    setError(null);
    try {
      const response = await api.sendChatMessage(
        content,
        mode,
        activeConversation?.pendingGenerationRequestId,
        conversationId,
      );
      removeMessageFromConversation(conversationId, thinkingId);
      if (response.type === "direct_answer" || response.type === "unsafe_or_unsupported") {
        appendMessagesToConversation(conversationId, [{ id: nextId + 2, role: "assistant", content: response.message }]);
      } else if (response.type === "project_not_plausible") {
        updateConversation(conversationId, (conversation) => ({
          ...conversation,
          pendingGenerationRequestId: undefined,
          updatedAt: new Date().toISOString(),
        }));
        appendMessagesToConversation(conversationId, [
          {
            id: nextId + 2,
            role: "assistant",
            content: `${response.message}\n\n${response.reason}`,
          },
        ]);
      } else if (response.type === "project_needs_input") {
        updateConversation(conversationId, (conversation) => ({
          ...conversation,
          pendingGenerationRequestId: response.generation_request.id,
          updatedAt: new Date().toISOString(),
        }));
        appendMessagesToConversation(conversationId, [
          {
            id: nextId + 2,
            role: "assistant",
            content: response.question || response.message,
          },
        ]);
      } else {
        const generationRequest = response.generation_request;
        if (!generationRequest || typeof generationRequest.id !== "number") {
          throw new Error("The backend returned an invalid skill generation plan. Please refresh and try again.");
        }
        updateConversation(conversationId, (conversation) => ({
          ...conversation,
          pendingGenerationRequestId: undefined,
          updatedAt: new Date().toISOString(),
        }));
        setApprovalResult(null);
        const displayName =
          generationRequest.proposed_display_name || generationRequest.proposed_skill_name || "this skill";
        appendMessagesToConversation(conversationId, [
          {
            id: nextId + 2,
            role: "assistant",
            kind: "build_approval",
            generationRequest,
            permissionRequest: response.permission_request,
            actionStatus: "pending",
            content: buildApprovalMessage(displayName),
          },
        ]);
      }
    } catch (err) {
      removeMessageFromConversation(conversationId, thinkingId);
      const message = err instanceof Error ? err.message : "Could not send chat message";
      appendMessagesToConversation(conversationId, [{ id: nextId + 3, role: "assistant", content: message }]);
      setError(message);
    } finally {
      setIsSending(false);
    }
  }

  async function handleApprove(message: ChatMessage) {
    if (!message.generationRequest) return;
    const conversationId = activeConversationId;
    const requestId = message.generationRequest.id;
    if (typeof requestId !== "number") {
      setError("This generation request is missing an id. Please send the project request again.");
      return;
    }
    updateMessageInConversation(conversationId, message.id, (current) => ({ ...current, actionStatus: "working" }));
    setIsGenerating(true);
    setError(null);
    try {
      const result = await api.approveSkillGeneration(requestId);
      setApprovalResult(result);
      updateMessageInConversation(conversationId, message.id, (current) => ({
        ...current,
        actionStatus: "approved",
        content: `${current.content}\n\nApproved. Builder and Tester are running the controlled workflow now.`,
      }));
      const runtimeRequest = result.runtime_permission_request ??
        (result.proposed_skill
          ? (await api.listPermissionRequests({ skill_id: result.proposed_skill.id, request_scope: "runtime" }))[0] ?? null
          : null);
      appendMessagesToConversation(conversationId, [
        {
          id: Date.now(),
          role: "assistant",
          kind: "generation_result",
          skill: result.proposed_skill ?? undefined,
          validation: result.validation,
          agentRun: result.agent_run,
          content: result.proposed_skill
            ? generationResultMessage(result.proposed_skill.name, result.agent_run, result.validation)
            : "Generation finished without a proposed skill record.",
        },
      ]);
      if (result.proposed_skill && runtimeRequest) {
        appendMessagesToConversation(conversationId, [
          {
            id: Date.now() + 1,
            role: "assistant",
            kind: "runtime_approval",
            skill: result.proposed_skill,
            permissionRequest: runtimeRequest,
            actionStatus: runtimeRequest.status === "pending" ? "pending" : runtimeRequest.status,
            content: runtimeApprovalMessage(result.proposed_skill.name, runtimeRequest),
          },
        ]);
      }
    } catch (err) {
      updateMessageInConversation(conversationId, message.id, (current) => ({ ...current, actionStatus: "failed" }));
      setError(err instanceof Error ? err.message : "Could not approve generation");
    } finally {
      setIsGenerating(false);
    }
  }

  async function handleDeny(message: ChatMessage) {
    if (!message.generationRequest) return;
    const conversationId = activeConversationId;
    const requestId = message.generationRequest.id;
    if (typeof requestId !== "number") {
      setError("This generation request is missing an id. Please send the project request again.");
      return;
    }
    updateMessageInConversation(conversationId, message.id, (current) => ({ ...current, actionStatus: "working" }));
    setError(null);
    try {
      await api.denySkillGeneration(requestId);
      updateMessageInConversation(conversationId, message.id, (current) => ({
        ...current,
        actionStatus: "denied",
        content: `${current.content}\n\nDeclined. No files were generated.`,
      }));
      appendMessagesToConversation(conversationId, [
        {
          id: Date.now(),
          role: "assistant",
          content: "Cancelled skill generation. No files were generated.",
        },
      ]);
    } catch (err) {
      updateMessageInConversation(conversationId, message.id, (current) => ({ ...current, actionStatus: "failed" }));
      setError(err instanceof Error ? err.message : "Could not deny generation");
    }
  }

  async function handleApproveRuntime(message: ChatMessage) {
    if (!message.skill) return;
    const conversationId = activeConversationId;
    updateMessageInConversation(conversationId, message.id, (current) => ({ ...current, actionStatus: "working" }));
    setError(null);
    try {
      const request = await api.approveRuntimePermissions(message.skill.id);
      updateMessageInConversation(conversationId, message.id, (current) => ({
        ...current,
        permissionRequest: request,
        actionStatus: "approved",
        content: `${runtimeApprovalMessage(message.skill?.name ?? "this skill", request)}\n\nApproved. This does not install or run the skill automatically.`,
      }));
    } catch (err) {
      updateMessageInConversation(conversationId, message.id, (current) => ({ ...current, actionStatus: "failed" }));
      setError(err instanceof Error ? err.message : "Could not approve runtime permissions");
    }
  }

  async function handleDenyRuntime(message: ChatMessage) {
    if (!message.skill) return;
    const conversationId = activeConversationId;
    updateMessageInConversation(conversationId, message.id, (current) => ({ ...current, actionStatus: "working" }));
    setError(null);
    try {
      const request = await api.denyRuntimePermissions(message.skill.id);
      updateMessageInConversation(conversationId, message.id, (current) => ({
        ...current,
        permissionRequest: request,
        actionStatus: "denied",
        content: `${runtimeApprovalMessage(message.skill?.name ?? "this skill", request)}\n\nDenied. Installation and execution stay blocked.`,
      }));
    } catch (err) {
      updateMessageInConversation(conversationId, message.id, (current) => ({ ...current, actionStatus: "failed" }));
      setError(err instanceof Error ? err.message : "Could not deny runtime permissions");
    }
  }

  return (
    <section className="page stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">Conversation</p>
          <h1>Chat</h1>
        </div>
      </header>

      <div className="chat-workspace">
        <aside className="chat-sidebar" aria-label="Chats">
          <button type="button" onClick={handleNewChat}>
            New Chat
          </button>
          <div className="chat-thread-list">
            {conversations.map((conversation) => (
              <div
                key={conversation.id}
                className={`chat-thread-row ${conversation.id === activeConversationId ? "active" : ""}`}
              >
                <button
                  type="button"
                  className="chat-thread-select"
                  onClick={() => selectConversation(conversation.id)}
                >
                  <strong>{conversation.title}</strong>
                  <span>{new Date(conversation.updatedAt).toLocaleString()}</span>
                </button>
                <button
                  type="button"
                  className="chat-thread-delete"
                  onClick={() => handleDeleteConversation(conversation.id)}
                  disabled={isBusy}
                  aria-label={`Delete chat ${conversation.title}`}
                  title="Delete chat"
                >
                  Delete
                </button>
              </div>
            ))}
          </div>
        </aside>
        <div className="chat-panel">
        <div className="chat-mode-bar">
          <span className="muted">
            {mode === "project"
              ? "Project mode creates proposed skills after approval."
              : "Chat mode asks Codex for normal answers and will not build skills."}
          </span>
          <div className="segmented-control" aria-label="Chat mode">
            <button
              type="button"
              className={mode === "chat" ? "active" : ""}
              onClick={() => updateMode("chat")}
              aria-pressed={mode === "chat"}
            >
              Chat
            </button>
            <button
              type="button"
              className={mode === "project" ? "active" : ""}
              onClick={() => updateMode("project")}
              aria-pressed={mode === "project"}
            >
              Project
            </button>
          </div>
        </div>
        <div className="message-list" aria-live="polite">
          {messages.map((message) => (
            <article key={message.id} className={`message ${message.role}`}>
              <span>{message.role}</span>
              <MessageBody
                message={message}
                isWorking={isGenerating}
                onApproveBuild={handleApprove}
                onDenyBuild={handleDeny}
                onApproveRuntime={handleApproveRuntime}
                onDenyRuntime={handleDenyRuntime}
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
        <form className="composer" onSubmit={handleSubmit}>
          <input
            value={draft}
            onChange={(event) => updateDraft(event.target.value)}
            placeholder={
              isBusy
                ? "Project build is running..."
                : mode === "project"
                  ? "Describe the skill you want to propose"
                  : "Type a message"
            }
            aria-label="Chat message"
            disabled={isBusy && mode === "project"}
          />
          <button type="submit" disabled={isBusy}>
            {isSending ? "Sending..." : "Send"}
          </button>
        </form>
      </div>
      </div>
      {error && <p className="error-text">{error}</p>}
      {approvalResult?.proposed_skill && (
        <section className="detail-panel">
          <h2>Generated Proposed Skill</h2>
          <p>
            <Link to={`/skills/${approvalResult.proposed_skill.id}`}>
              Open {approvalResult.proposed_skill.name}
            </Link>
          </p>
          {approvalResult.validation && (
            <p className="muted">
              Validation {approvalResult.validation.ok ? "passed" : "needs attention"}. The skill is still proposed.
            </p>
          )}
        </section>
      )}
    </section>
  );
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
        {message.skill && (
          <p>
            <Link to={`/skills/${message.skill.id}`}>Open {message.skill.name}</Link>
          </p>
        )}
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
        {message.skill && (
          <p>
            <Link to={`/skills/${message.skill.id}`}>Open {message.skill.name}</Link>
          </p>
        )}
        {message.agentRun && (
          <p>
            <Link to={`/agent-runs/${message.agentRun.id}`}>Open Agent Run #{message.agentRun.id}</Link>
          </p>
        )}
        {message.validation && (
          <p className="muted">Validation {message.validation.ok ? "passed" : "needs attention"}.</p>
        )}
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
  const hasExpansion = isNonEmptyObject(expansion);
  return (
    <div className="permission-inline-summary">
      {pmSummary && (
        <section>
          <h3>ProductManager Summary</h3>
          <p>{pmSummary}</p>
        </section>
      )}
      {permissionSummary && (
        <section>
          <h3>Permission Review</h3>
          <p>{permissionSummary}</p>
        </section>
      )}
      <section>
        <h3>Requested Access</h3>
        <div className="chip-list">
          {summarizePermissionLabels(request).map((label) => (
            <span key={label} className="permission-chip">{label}</span>
          ))}
        </div>
      </section>
      {hasExpansion && (
        <p className="error-text">Permission expansion detected: {JSON.stringify(expansion)}</p>
      )}
      <p className="muted">
        Approval lets the next controlled step continue. It does not install the skill, run the skill, or grant automatic execution.
      </p>
    </div>
  );
}

function buildApprovalMessage(displayName: string): string {
  return `I prepared a skill blueprint for ${displayName}. Review the ProductManager summary and permission review below, then approve or decline generation.`;
}

function generationResultMessage(
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

function runtimeApprovalMessage(skillName: string, request: ApprovalRequest): string {
  const hasExpansion = isNonEmptyObject(request.reason_json.permission_expansion);
  const expansionText = hasExpansion ? " Permission expansion was detected and must be reviewed." : "";
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
  if (request.requested_network_domains_json.length) {
    labels.push(`Network: ${request.requested_network_domains_json.join(", ")}`);
  }
  if (request.requested_dependencies_json.length) {
    labels.push(`Packages: ${request.requested_dependencies_json.join(", ")}`);
  }
  const filesystem = request.requested_filesystem_json;
  if (Array.isArray(filesystem.filesystem_read) && filesystem.filesystem_read.length) {
    labels.push(`Read: ${filesystem.filesystem_read.join(", ")}`);
  }
  if (Array.isArray(filesystem.filesystem_write) && filesystem.filesystem_write.length) {
    labels.push(`Write: ${filesystem.filesystem_write.join(", ")}`);
  }
  const permissions = request.requested_permissions_json;
  if (permissions.codex_generation) labels.push("Codex generation");
  if (permissions.internet_research) labels.push("Internet research");
  return labels.length ? labels : ["No special permissions"];
}

function isNonEmptyObject(value: unknown): boolean {
  return typeof value === "object" && value !== null && !Array.isArray(value) && Object.keys(value).length > 0;
}
