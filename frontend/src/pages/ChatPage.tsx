import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";

import PermissionRequestModal from "../components/PermissionRequestModal";
import { ApprovalRequest, ChatMode, SkillGenerationApprovalResponse, SkillGenerationRequest, api } from "../api/client";

type ChatMessage = {
  id: number;
  role: "user" | "assistant";
  content: string;
};

const initialMessages: ChatMessage[] = [
  {
    id: 1,
    role: "assistant",
    content: "Use Chat mode for normal conversation. Switch to Project mode when you want me to propose a reusable skill.",
  },
];

export default function ChatPage() {
  const [messages, setMessages] = useState<ChatMessage[]>(initialMessages);
  const [draft, setDraft] = useState("");
  const [mode, setMode] = useState<ChatMode>("chat");
  const [pendingRequest, setPendingRequest] = useState<SkillGenerationRequest | null>(null);
  const [pendingPermissionRequest, setPendingPermissionRequest] = useState<ApprovalRequest | null>(null);
  const [approvalResult, setApprovalResult] = useState<SkillGenerationApprovalResponse | null>(null);
  const [isSending, setIsSending] = useState(false);
  const [isGenerating, setIsGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const content = draft.trim();
    if (!content) return;

    const nextId = Date.now();
    setMessages((current) => [
      ...current,
      { id: nextId, role: "user", content },
    ]);
    setDraft("");
    setIsSending(true);
    setError(null);
    try {
      const response = await api.sendChatMessage(content, mode);
      if (response.type === "direct_answer" || response.type === "unsafe_or_unsupported") {
        setMessages((current) => [
          ...current,
          { id: nextId + 1, role: "assistant", content: response.message },
        ]);
      } else if (response.type === "project_not_plausible") {
        const optionalProjects = response.optional_projects.length
          ? `\n\nOptional projects:\n${response.optional_projects.map((project) => `- ${project}`).join("\n")}`
          : "";
        setMessages((current) => [
          ...current,
          {
            id: nextId + 1,
            role: "assistant",
            content: `${response.message}\n\n${response.reason}${optionalProjects}`,
          },
        ]);
      } else {
        setPendingRequest(response.generation_request);
        setPendingPermissionRequest(response.permission_request);
        setApprovalResult(null);
        setMessages((current) => [
          ...current,
          {
            id: nextId + 1,
            role: "assistant",
            content: `I prepared a proposed skill generation plan for ${response.generation_request.proposed_display_name}.`,
          },
        ]);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not send chat message");
    } finally {
      setIsSending(false);
    }
  }

  async function handleApprove() {
    if (!pendingRequest) return;
    setIsGenerating(true);
    setError(null);
    try {
      const result = await api.approveSkillGeneration(pendingRequest.id);
      setApprovalResult(result);
      setPendingRequest(null);
      setPendingPermissionRequest(null);
      setMessages((current) => [
        ...current,
        {
          id: Date.now(),
          role: "assistant",
          content: result.proposed_skill
            ? `Generated proposed skill ${result.proposed_skill.name}. It has not been installed or run.`
            : "Generation finished without a proposed skill record.",
        },
      ]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not approve generation");
    } finally {
      setIsGenerating(false);
    }
  }

  async function handleDeny() {
    if (!pendingRequest) return;
    setError(null);
    try {
      await api.denySkillGeneration(pendingRequest.id);
      setMessages((current) => [
        ...current,
        {
          id: Date.now(),
          role: "assistant",
          content: "Cancelled skill generation. No files were generated.",
        },
      ]);
      setPendingRequest(null);
      setPendingPermissionRequest(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not deny generation");
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

      <div className="chat-panel">
        <div className="chat-mode-bar">
          <span className="muted">
            {mode === "project"
              ? "Project mode creates proposed skills after approval."
              : "Chat mode will not build skills."}
          </span>
          <div className="segmented-control" aria-label="Chat mode">
            <button
              type="button"
              className={mode === "chat" ? "active" : ""}
              onClick={() => setMode("chat")}
              aria-pressed={mode === "chat"}
            >
              Chat
            </button>
            <button
              type="button"
              className={mode === "project" ? "active" : ""}
              onClick={() => setMode("project")}
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
              <p>{message.content}</p>
            </article>
          ))}
        </div>
        <form className="composer" onSubmit={handleSubmit}>
          <input
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder={mode === "project" ? "Describe the skill you want to propose" : "Type a message"}
            aria-label="Chat message"
          />
          <button type="submit" disabled={isSending}>
            {isSending ? "Sending..." : "Send"}
          </button>
        </form>
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
      {pendingRequest && pendingPermissionRequest && (
        <PermissionRequestModal
          request={pendingPermissionRequest}
          title={pendingRequest.proposed_display_name}
          subject="Approving this lets Codex generate a proposed skill. It does not install or run it."
          isWorking={isGenerating}
          approveLabel="Approve Generation"
          denyLabel="Decline"
          onApprove={handleApprove}
          onDeny={handleDeny}
        />
      )}
    </section>
  );
}
