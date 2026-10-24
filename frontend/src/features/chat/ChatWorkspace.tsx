import { FormEvent, Fragment, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import {
  AgentRun,
  ApprovalRequest,
  ConversationMode,
  ProposedSkillValidation,
} from "../../api/client";
import { DeleteIconButton } from "../../components/DeleteIconButton";
import { MessageMarkdown } from "../../components/MessageMarkdown";
import { AppIcon } from "../../components/AppIcon";
import eidolonMark from "../../../resource/logo/eidolon-mark.svg";
import "./conversations.css";
import { ChatConversation, ChatMessage } from "../../lib/chatStore";
import { parseBackendDateTime } from "../../lib/dateTime";
import { messageTimestampSeparators } from "./messageTimeline";

type ChatWorkspaceProps = {
  embedded?: boolean;
  hideSidebar?: boolean;
  title?: string;
  description?: string;
  allowedModes?: ConversationMode[];
  conversations: ChatConversation[];
  activeConversationId: string;
  messages: ChatMessage[];
  draft: string;
  mode: ConversationMode;
  isBusy: boolean;
  historyLoading?: boolean;
  historyError?: boolean;
  onRetryHistory?: () => void;
  canReplyToAct?: boolean;
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
  embedded = false,
  hideSidebar = false,
  title = "Chat",
  description,
  allowedModes,
  conversations,
  activeConversationId,
  messages,
  draft,
  mode,
  isBusy,
  historyLoading = false,
  historyError = false,
  onRetryHistory,
  canReplyToAct = false,
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
  const activeConversation = conversations.find((conversation) => conversation.id === activeConversationId);
  return (
    <section className={embedded ? "chat-page embedded-chat" : "page stack chat-page"} aria-label={`${title} conversation`}>
      {!embedded && <header className="page-header">
        <div>
          <h1>{title}</h1>
          {description && <p className="muted">{description}</p>}
        </div>
      </header>}

      <div className={`chat-workspace ${hideSidebar ? "single-conversation" : ""}`}>
        {!hideSidebar && <ChatSidebar
          conversations={conversations}
          activeConversationId={activeConversationId}
          onNewConversation={onNewConversation}
          onSelectConversation={onSelectConversation}
          allowedModes={allowedModes}
        />}
        <div className="chat-panel">
          <ConversationModeBar
            conversation={activeConversation}
            isBusy={isBusy}
            onDeleteConversation={onDeleteConversation}
            showTitle={Boolean(allowedModes)}
          />
          <MessageList
            key={activeConversationId}
            mode={mode}
            onDraftChange={onDraftChange}
            messages={messages}
            historyLoading={historyLoading}
            historyError={historyError}
            onRetryHistory={onRetryHistory}
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
            historyLoading={historyLoading || historyError}
            canReplyToAct={canReplyToAct}
            isSending={isSending}
            onDraftChange={onDraftChange}
            onSubmit={onSubmit}
            activeActTurnId={activeActTurnId}
            onCancelAct={onCancelAct}
          />
        </div>
      </div>
      {error && <p className="error-text" role="alert">{error}</p>}
    </section>
  );
}

function ChatSidebar({
  conversations,
  activeConversationId,
  onNewConversation,
  onSelectConversation,
  allowedModes,
}: Pick<
  ChatWorkspaceProps,
  | "conversations"
  | "activeConversationId"
  | "onNewConversation"
  | "onSelectConversation"
  | "allowedModes"
>) {
  const [isCreatePopoverOpen, setIsCreatePopoverOpen] = useState(false);
  const [compact, setCompact] = useState(() => window.matchMedia?.("(max-width: 640px)").matches ?? false);
  const [threadsOpen, setThreadsOpen] = useState(false);
  const [threadSearch, setThreadSearch] = useState("");
  const threadsId = useId();
  useEffect(() => {
    const media = window.matchMedia?.("(max-width: 640px)");
    const update = () => setCompact(media?.matches ?? false);
    media?.addEventListener("change", update);
    return () => media?.removeEventListener("change", update);
  }, []);
  const threadsCollapsed = Boolean(allowedModes) && compact && !threadsOpen;
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
    <aside className={`chat-sidebar ${threadsCollapsed ? "threads-collapsed" : ""}`} aria-label={allowedModes?.[0] === "project" ? "Skill builds" : "Chats"}>
      {allowedModes && <div className="chat-sidebar-heading"><strong>{allowedModes[0] === "project" ? "Your builds" : "Conversations"}</strong><span className="muted">{conversations.length}</span><button type="button" className="secondary compact chat-thread-toggle" aria-label={threadsOpen ? "Hide conversations" : "Show conversations"} aria-expanded={threadsOpen} aria-controls={threadsId} onClick={() => setThreadsOpen((open) => !open)}>{threadsOpen ? "−" : "⌄"}</button></div>}
      <div className="thread-search" hidden={threadsCollapsed}><AppIcon name="search" size={15} /><input type="search" aria-label="Search conversations" placeholder="Search conversations…" value={threadSearch} onChange={(event) => setThreadSearch(event.target.value)} /></div>
      <div id={threadsId} className="chat-thread-list" hidden={threadsCollapsed}>
        {conversations.filter((conversation) => conversation.title.toLowerCase().includes(threadSearch.toLowerCase().trim())).map((conversation) => (
          <div
            key={conversation.id}
            className={`chat-thread-row ${conversation.id === activeConversationId ? "active" : ""}`}
          >
            <button
              type="button"
              className="chat-thread-select"
              onClick={() => { onSelectConversation(conversation.id); setThreadsOpen(false); }}
              aria-pressed={conversation.id === activeConversationId}
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
        {threadSearch && !conversations.some((conversation) => conversation.title.toLowerCase().includes(threadSearch.toLowerCase().trim())) && <p className="thread-empty muted">No matching conversations.</p>}
      </div>
      <div className="chat-create-actions" aria-label="Create conversation">
        {allowedModes?.length === 1 ? <button type="button" onClick={() => { onNewConversation(allowedModes[0]); setThreadsOpen(false); }}>
          <span aria-hidden="true">＋</span> {allowedModes[0] === "project" ? "New build" : "New conversation"}
        </button> : <button
          ref={createButtonRef}
          type="button"
          aria-haspopup="dialog"
          aria-expanded={isCreatePopoverOpen}
          aria-controls="chat-create-popover"
          onClick={() => setIsCreatePopoverOpen((open) => !open)}
        >
          <span className="conversation-mode-symbol" aria-hidden="true">＋</span>
          <span>New Chat</span>
        </button>}
        {isCreatePopoverOpen && (
          <div className="chat-create-popover-layer">
            <aside
              id="chat-create-popover"
              className="chat-create-popover"
              ref={createPopoverRef}
              role="dialog"
              aria-modal="true"
              aria-label="Create conversation"
            >
              <div className="chat-create-options">
                {conversationModeOptions.filter((option) => !allowedModes || allowedModes.includes(option.mode)).map((option, index) => (
                  <button
                    key={option.mode}
                    ref={index === 0 ? firstCreateOptionRef : undefined}
                    type="button"
                    className={`chat-create-option ${option.mode}`}
                    aria-label={`${option.mode === "assistant" ? "Open" : "Create"} ${option.label}`}
                    onClick={() => {
                      onNewConversation(option.mode);
                      closeCreatePopover();
                    }}
                  >
                    <span className="conversation-mode-symbol" aria-hidden="true">{modeSymbol(option.mode)}</span>
                    <span>
                      <strong>{option.mode === "assistant" ? "Open" : "Create"} {option.label}</strong>
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
  conversation,
  isBusy,
  onDeleteConversation,
  showTitle,
}: {
  conversation: ChatConversation | undefined;
  isBusy: boolean;
  onDeleteConversation: (conversationId: string) => void;
  showTitle: boolean;
}) {
  if (!conversation) return null;
  return (
    <div className="chat-mode-bar">
      <span className="chat-conversation-heading"><ConversationModeMarker mode={conversation.mode} />{showTitle && <strong>{conversation.mode === "assistant" ? "Your ongoing conversation" : conversation.title}</strong>}</span>
      <span className="chat-session-metadata">
        <span>Created {formatSessionTimestamp(conversation.createdAt)}</span>
        {conversation.origin === "wecom" && <span>WeCom user {conversation.wecomUserId ?? "unknown"}</span>}
      </span>
      {conversation.mode !== "assistant" && <DeleteIconButton
        onClick={() => onDeleteConversation(conversation.id)}
        disabled={isBusy}
        label="Delete conversation"
      />}
    </div>
  );
}

function MessageList({
  mode,
  onDraftChange,
  messages,
  historyLoading,
  historyError,
  onRetryHistory,
  isSending,
  isGenerating,
  onApproveBuild,
  onDenyBuild,
  onApproveRuntime,
  onDenyRuntime,
}: Pick<
  ChatWorkspaceProps,
  | "mode"
  | "onDraftChange"
  | "messages"
  | "historyLoading"
  | "historyError"
  | "onRetryHistory"
  | "isSending"
  | "isGenerating"
  | "onApproveBuild"
  | "onDenyBuild"
  | "onApproveRuntime"
  | "onDenyRuntime"
>) {
  const list = useRef<HTMLDivElement>(null);
  const followingLatest = useRef(true);
  const timestampSeparators = messageTimestampSeparators(messages);
  const showWelcome = !historyLoading && !historyError && !isSending && !isGenerating && (messages.length === 0 || (messages.length === 1 && /^Hi, what can I (?:do|build) for you/.test(messages[0].content)));
  const suggestions = mode === "project" ? [
    ["Start with an idea", "I want to build a skill that "], ["Automate a routine", "Help me design a reusable skill to automate "],
  ] : mode === "act" ? [
    ["Work on a task", "Help me complete this task: "], ["Research something", "Research and summarize "],
  ] : mode === "observer" ? [
    ["Review my context", "Review my current context and summarize what needs attention."], ["Explore a question", "Help me understand "],
  ] : [
    ["Plan my day", "Help me plan my day based on what needs my attention."], ["Think it through", "Help me think through "],
    ["Catch me up", "What has changed recently that I should know about?"], ["Find the next step", "Help me choose the next useful step on my current goals."],
  ];
  useLayoutEffect(() => {
    if (followingLatest.current && list.current) list.current.scrollTop = list.current.scrollHeight;
  }, [messages, isSending, isGenerating, historyLoading]);
  return (
    <div ref={list} className="message-list" role="log" aria-label="Conversation messages" aria-live="polite" aria-busy={historyLoading} onScroll={() => {
      const element = list.current;
      if (element) followingLatest.current = element.scrollHeight - element.scrollTop - element.clientHeight < 140;
    }}>
      {showWelcome && <div className="conversation-welcome"><img src={eidolonMark} alt="" width="48" height="48" /><h2>{messages[0]?.content ?? (mode === "project" ? "What would you like to build?" : "What can we work on?")}</h2><div className="conversation-suggestions">{suggestions.map(([label, prompt]) => <button type="button" className="secondary" key={label} onClick={() => { onDraftChange(prompt); document.querySelector<HTMLTextAreaElement>(".composer textarea")?.focus(); }}><span>{label}</span><AppIcon name="arrow" size={15} /></button>)}</div></div>}
      {historyLoading && <div className="conversation-history-state" role="status"><span className="thinking-spinner" aria-hidden="true" />Loading conversation…</div>}
      {historyError && <div className="conversation-history-state"><p role="alert">Could not load this conversation.</p><button type="button" className="secondary compact" onClick={onRetryHistory}>Retry conversation</button></div>}
      {!showWelcome && !historyLoading && !historyError && messages.map((message) => (
        <Fragment key={message.id}>
          {timestampSeparators.has(message.id) && <div className="chat-timestamp-separator">
            <time dateTime={timestampSeparators.get(message.id)!.toISOString()}>
              {formatSessionTimestamp(message.createdAt!)}
            </time>
          </div>}
        <article className={`message ${message.role}`}>
          <span className="message-author">{message.role === "assistant" && <img src={eidolonMark} alt="" width="20" height="20" />}{message.role === "assistant" ? "Eidolon" : "You"}</span>
          <MessageBody
            message={message}
            isWorking={isGenerating}
            onApproveBuild={onApproveBuild}
            onDenyBuild={onDenyBuild}
            onApproveRuntime={onApproveRuntime}
            onDenyRuntime={onDenyRuntime}
          />
        </article>
        </Fragment>
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
  historyLoading,
  canReplyToAct,
  isSending,
  onDraftChange,
  onSubmit,
  activeActTurnId,
  onCancelAct,
}: Pick<
  ChatWorkspaceProps,
  "draft" | "mode" | "isBusy" | "historyLoading" | "canReplyToAct" | "isSending" | "onDraftChange" | "onSubmit" | "activeActTurnId" | "onCancelAct"
>) {
  const acceptingReply = (mode === "act" || mode === "assistant") && canReplyToAct && !isSending;
  return (
    <div className="composer-dock"><form className="composer" onSubmit={onSubmit}>
      <textarea
        rows={1}
        value={draft}
        onChange={(event) => onDraftChange(event.target.value)}
        placeholder={
          acceptingReply ? `Reply to ${modeLabel(mode)} while it waits...` : isBusy
            ? mode === "project" ? "Project build is running..." : `${modeLabel(mode)} is thinking...`
            : mode === "project"
              ? "Describe the skill you want to propose"
              : `Ask ${modeLabel(mode)} ${mode === "act" ? "to work in its workspace" : mode === "observer" ? "to inspect local context" : "for help or talk things through"}`
        }
        aria-label="Conversation message"
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
            event.preventDefault();
            if (!isBusy || acceptingReply) event.currentTarget.form?.requestSubmit();
          }
        }}
        disabled={isBusy && mode === "project"}
      />
      <button
        type="submit"
        className="chat-send-button"
        disabled={historyLoading || (isBusy && !acceptingReply)}
        aria-label={isSending ? "Sending message" : "Send message"}
        title={isSending ? "Sending" : "Send message"}
      >
        {isSending ? (
          <span className="thinking-spinner" aria-hidden="true" />
        ) : (
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M12 19V5m-6 6 6-6 6 6" />
          </svg>
        )}
      </button>
      {mode !== "project" && activeActTurnId !== null && (
        <button type="button" className="secondary" onClick={onCancelAct}>Cancel</button>
      )}
    </form></div>
  );
}

function modeLabel(mode: ConversationMode): string {
  return conversationModeOptions.find((option) => option.mode === mode)?.label ?? mode;
}

function modeSymbol(mode: ConversationMode): string {
  return mode === "project" ? "◇" : mode === "act" ? "↯" : mode === "observer" ? "◉" : "✦";
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
  { mode: "assistant", label: "Assistant", description: "Your primary assistant for conversation and ongoing help." },
  { mode: "project", label: "Project", description: "Creates proposed skills after approval." },
  { mode: "act", label: "Act", description: "Works persistently in the shared workspace with Eidolon tools." },
  { mode: "observer", label: "Observer", description: "Reads local context without making changes." },
];

function formatSessionTimestamp(value: string): string {
  const date = parseBackendDateTime(value);
  if (Number.isNaN(date.getTime())) return "unknown";
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
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
        {message.agentRun && <p><Link to={`/activity/runs/${message.agentRun.id}`}>Open execution #{message.agentRun.id}</Link></p>}
        {message.validation && <p className="muted">Validation {message.validation.ok ? "passed" : "needs attention"}.</p>}
      </div>
    );
  }
  return (
    <div className="message-body">
      {message.role === "assistant" ? <MessageMarkdown content={message.content} /> : <p>{message.content}</p>}
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

  const startedAt = parseBackendDateTime(work.startedAt).getTime();
  const completedAt = work.completedAt ? parseBackendDateTime(work.completedAt).getTime() : now;
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
