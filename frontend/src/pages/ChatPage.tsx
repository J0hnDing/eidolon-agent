import { FormEvent, useEffect, useState } from "react";

import { ActSession, ActTurn, ChatMode, api } from "../api/client";
import {
  ChatWorkspace,
  buildApprovalMessage,
  generationResultMessage,
  runtimeApprovalMessage,
} from "../features/chat/ChatWorkspace";
import { useChatConversations } from "../features/chat/useChatConversations";
import { mergeProjectConversationState } from "../features/chat/projectConversationState";
import { ChatMessage, initialMessagesForMode } from "../lib/chatStore";
import { usePolling } from "../lib/usePolling";

export default function ChatPage() {
  const chat = useChatConversations();
  const [isSending, setIsSending] = useState(false);
  const [isGenerating, setIsGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [projectStateNeedsPolling, setProjectStateNeedsPolling] = useState(false);
  const [actStateNeedsPolling, setActStateNeedsPolling] = useState(false);
  const [activeActTurnId, setActiveActTurnId] = useState<number | null>(null);
  const hasPendingMessage = chat.messages.some(
    (message) => message.kind === "thinking" || message.actionStatus === "working",
  );
  const isBusy = isSending || isGenerating || hasPendingMessage;

  async function syncProjectConversation(conversationId: string): Promise<boolean> {
    try {
      const state = await api.getProjectConversationState(conversationId);
      setProjectStateNeedsPolling(Boolean(state?.needs_polling));
      if (!state) return false;
      chat.updateConversation(conversationId, (conversation) => {
        const messages = mergeProjectConversationState(conversation.messages, state);
        if (JSON.stringify(messages) === JSON.stringify(conversation.messages)) return conversation;
        return { ...conversation, messages, updatedAt: new Date().toISOString() };
      });
      return true;
    } catch {
      return false;
    }
  }

  useEffect(() => {
    setProjectStateNeedsPolling(chat.mode === "project");
    if (chat.mode === "project") void syncProjectConversation(chat.activeConversationId);
    // Conversation synchronization is keyed only by the active id and mode.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chat.activeConversationId, chat.mode]);

  usePolling(
    async () => {
      await syncProjectConversation(chat.activeConversationId);
    },
    chat.mode === "project" && projectStateNeedsPolling,
    3000,
  );

  async function syncActConversation(conversationId: string, sessionId: number): Promise<boolean> {
    try {
      const session = await api.getActSession(sessionId);
      const activeTurn = [...session.turns].reverse().find(isActiveActTurn) ?? null;
      setActStateNeedsPolling(Boolean(activeTurn));
      if (conversationId === chat.activeConversationId) setActiveActTurnId(activeTurn?.id ?? null);
      chat.updateConversation(conversationId, (conversation) => ({
        ...conversation,
        title: session.title,
        messages: actSessionMessages(session),
        updatedAt: session.updated_at,
      }));
      return true;
    } catch {
      return false;
    }
  }

  useEffect(() => {
    void api.listActSessions()
      .then(chat.importActSessions)
      .catch((reason) => setError(reason instanceof Error ? reason.message : "Could not load Act conversations"));
    // Active Act sessions are imported once when the shared conversation page mounts.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    setActiveActTurnId(null);
    setActStateNeedsPolling(false);
    const sessionId = chat.activeConversation?.actSessionId;
    if (chat.mode === "act" && sessionId !== undefined) {
      void syncActConversation(chat.activeConversationId, sessionId);
    }
    // Act synchronization is keyed only by the active local conversation binding.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chat.activeConversationId, chat.mode, chat.activeConversation?.actSessionId]);

  usePolling(
    async () => {
      const sessionId = chat.activeConversation?.actSessionId;
      if (sessionId !== undefined) await syncActConversation(chat.activeConversationId, sessionId);
    },
    chat.mode === "act" && actStateNeedsPolling,
    1000,
  );

  async function handleNewConversation(mode: ChatMode) {
    setError(null);
    if (mode !== "act") {
      chat.createNewConversation(mode);
      return;
    }
    setIsSending(true);
    try {
      const session = await api.createActSession();
      chat.createNewConversation("act", { actSessionId: session.id, title: session.title });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not create Act conversation");
    } finally {
      setIsSending(false);
    }
  }

  async function handleDeleteConversation(conversationId: string) {
    const conversation = chat.conversations.find((item) => item.id === conversationId);
    if (!conversation) return;
    setError(null);
    if (conversation.mode === "act" && conversation.actSessionId !== undefined) {
      try {
        await api.archiveActSession(conversation.actSessionId);
        chat.deleteConversation(conversationId);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not archive Act conversation");
      }
      return;
    }
    chat.deleteConversation(conversationId);
    try {
      await api.deleteChatConversation(conversationId);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Could not clean up backend chat history";
      setError(`Deleted the local chat, but backend history cleanup failed: ${message}`);
    }
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const content = chat.draft.trim();
    if (!content) return;

    const conversationId = chat.activeConversationId;
    const mode = chat.mode;
    const actSessionId = chat.activeConversation?.actSessionId;
    const nextId = Date.now();
    const thinkingId = nextId + 1;
    chat.appendMessagesToConversation(conversationId, [
      { id: nextId, role: "user", content },
      {
        id: thinkingId,
        role: "assistant",
        content: mode === "project" ? "ProductManager is reviewing the project..." : mode === "act" ? "Act is working..." : "Codex is thinking...",
        kind: "thinking",
      },
    ]);
    chat.updateConversation(conversationId, (conversation) => ({ ...conversation, draft: "" }));
    setIsSending(true);
    setError(null);
    try {
      if (mode === "act") {
        if (actSessionId === undefined) throw new Error("This Act conversation is not connected to a session.");
        await api.runActTurn(actSessionId, content);
        setActStateNeedsPolling(true);
        await syncActConversation(conversationId, actSessionId);
        return;
      }
      const response = await api.sendChatMessage(
        content,
        mode,
        chat.activeConversation?.pendingGenerationRequestId,
        conversationId,
      );
      chat.removeMessageFromConversation(conversationId, thinkingId);
      if (response.type === "direct_answer" || response.type === "unsafe_or_unsupported") {
        chat.appendMessagesToConversation(conversationId, [
          { id: nextId + 2, role: "assistant", content: response.message },
        ]);
      } else if (response.type === "project_not_plausible") {
        chat.updateConversation(conversationId, (conversation) => ({
          ...conversation,
          pendingGenerationRequestId: undefined,
          updatedAt: new Date().toISOString(),
        }));
        chat.appendMessagesToConversation(conversationId, [
          { id: nextId + 2, role: "assistant", content: `${response.message}\n\n${response.reason}` },
        ]);
      } else if (response.type === "project_needs_input") {
        chat.updateConversation(conversationId, (conversation) => ({
          ...conversation,
          pendingGenerationRequestId: response.generation_request.id,
          updatedAt: new Date().toISOString(),
        }));
        chat.appendMessagesToConversation(conversationId, [
          { id: nextId + 2, role: "assistant", content: response.question || response.message },
        ]);
      } else {
        const generationRequest = response.generation_request;
        if (!generationRequest || typeof generationRequest.id !== "number") {
          throw new Error("The backend returned an invalid skill generation plan. Please refresh and try again.");
        }
        chat.updateConversation(conversationId, (conversation) => ({
          ...conversation,
          pendingGenerationRequestId: undefined,
          updatedAt: new Date().toISOString(),
        }));
        const displayName =
          generationRequest.proposed_display_name || generationRequest.proposed_skill_name || "this skill";
        chat.appendMessagesToConversation(conversationId, [
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
      chat.removeMessageFromConversation(conversationId, thinkingId);
      if (mode === "act" && actSessionId !== undefined && await syncActConversation(conversationId, actSessionId)) {
        setError(null);
        return;
      }
      if (mode === "project" && await syncProjectConversation(conversationId)) {
        setError(null);
        return;
      }
      const message = err instanceof Error ? err.message : "Could not send chat message";
      chat.appendMessagesToConversation(conversationId, [
        { id: nextId + 3, role: "assistant", content: message },
      ]);
      setError(message);
    } finally {
      setIsSending(false);
    }
  }

  async function handleCancelAct() {
    const sessionId = chat.activeConversation?.actSessionId;
    if (sessionId === undefined || activeActTurnId === null) return;
    setError(null);
    try {
      await api.cancelActTurn(sessionId, activeActTurnId);
      await syncActConversation(chat.activeConversationId, sessionId);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not cancel the Act turn");
    }
  }

  async function handleApprove(message: ChatMessage) {
    if (!message.generationRequest) return;
    const conversationId = chat.activeConversationId;
    const requestId = message.generationRequest.id;
    if (typeof requestId !== "number") {
      setError("This generation request is missing an id. Please send the project request again.");
      return;
    }
    chat.updateMessageInConversation(conversationId, message.id, (current) => ({
      ...current,
      actionStatus: "working",
    }));
    setIsGenerating(true);
    setError(null);
    try {
      const result = await api.approveSkillGeneration(requestId);
      chat.updateMessageInConversation(conversationId, message.id, (current) => ({
        ...current,
        actionStatus: "approved",
        content: `${current.content}\n\nApproved. Builder and Tester are running the controlled workflow now.`,
      }));
      const runtimeRequest = result.runtime_permission_request ??
        (result.proposed_skill
          ? (await api.listPermissionRequests({ skill_id: result.proposed_skill.id, request_scope: "runtime" }))[0] ?? null
          : null);
      chat.appendMessagesToConversation(conversationId, [
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
        chat.appendMessagesToConversation(conversationId, [
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
      chat.updateMessageInConversation(conversationId, message.id, (current) => ({
        ...current,
        actionStatus: "failed",
      }));
      setError(err instanceof Error ? err.message : "Could not approve generation");
    } finally {
      setIsGenerating(false);
    }
  }

  async function handleDeny(message: ChatMessage) {
    if (!message.generationRequest) return;
    const conversationId = chat.activeConversationId;
    const requestId = message.generationRequest.id;
    if (typeof requestId !== "number") {
      setError("This generation request is missing an id. Please send the project request again.");
      return;
    }
    chat.updateMessageInConversation(conversationId, message.id, (current) => ({
      ...current,
      actionStatus: "working",
    }));
    setError(null);
    try {
      await api.denySkillGeneration(requestId);
      chat.updateMessageInConversation(conversationId, message.id, (current) => ({
        ...current,
        actionStatus: "denied",
        content: `${current.content}\n\nDeclined. No files were generated.`,
      }));
      chat.appendMessagesToConversation(conversationId, [
        { id: Date.now(), role: "assistant", content: "Cancelled skill generation. No files were generated." },
      ]);
    } catch (err) {
      chat.updateMessageInConversation(conversationId, message.id, (current) => ({
        ...current,
        actionStatus: "failed",
      }));
      setError(err instanceof Error ? err.message : "Could not deny generation");
    }
  }

  async function handleApproveRuntime(message: ChatMessage) {
    if (!message.skill || !message.permissionRequest) return;
    const conversationId = chat.activeConversationId;
    chat.updateMessageInConversation(conversationId, message.id, (current) => ({
      ...current,
      actionStatus: "working",
    }));
    setError(null);
    try {
      const request = await api.approveRuntimePermissions(message.skill.id);
      chat.updateMessageInConversation(conversationId, message.id, (current) => ({
        ...current,
        permissionRequest: request,
        actionStatus: "approved",
        content: `${runtimeApprovalMessage(message.skill?.name ?? "this skill", request)}\n\nApproved. This covers the complete runtime request shown, including its integration operations. It does not install or run the skill automatically.`,
      }));
    } catch (err) {
      chat.updateMessageInConversation(conversationId, message.id, (current) => ({
        ...current,
        actionStatus: "failed",
      }));
      setError(err instanceof Error ? err.message : "Could not approve runtime permissions");
    }
  }

  async function handleDenyRuntime(message: ChatMessage) {
    if (!message.skill || !message.permissionRequest) return;
    const conversationId = chat.activeConversationId;
    chat.updateMessageInConversation(conversationId, message.id, (current) => ({
      ...current,
      actionStatus: "working",
    }));
    setError(null);
    try {
      const request = await api.denyRuntimePermissions(message.skill.id);
      chat.updateMessageInConversation(conversationId, message.id, (current) => ({
        ...current,
        permissionRequest: request,
        actionStatus: "denied",
        content: `${runtimeApprovalMessage(message.skill?.name ?? "this skill", request)}\n\nDenied. Installation and execution stay blocked.`,
      }));
    } catch (err) {
      chat.updateMessageInConversation(conversationId, message.id, (current) => ({
        ...current,
        actionStatus: "failed",
      }));
      setError(err instanceof Error ? err.message : "Could not deny runtime permissions");
    }
  }

  return (
    <ChatWorkspace
      conversations={chat.conversations}
      activeConversationId={chat.activeConversationId}
      messages={chat.messages}
      draft={chat.draft}
      mode={chat.mode}
      isBusy={isBusy}
      isSending={isSending}
      isGenerating={isGenerating}
      error={error}
      onNewConversation={handleNewConversation}
      onSelectConversation={chat.selectConversation}
      onDeleteConversation={handleDeleteConversation}
      activeActTurnId={activeActTurnId}
      onCancelAct={handleCancelAct}
      onDraftChange={chat.updateDraft}
      onSubmit={handleSubmit}
      onApproveBuild={handleApprove}
      onDenyBuild={handleDeny}
      onApproveRuntime={handleApproveRuntime}
      onDenyRuntime={handleDenyRuntime}
    />
  );
}

function isActiveActTurn(turn: ActTurn): boolean {
  return turn.status === "queued" || turn.status === "running";
}

function actSessionMessages(session: ActSession): ChatMessage[] {
  return [
    ...initialMessagesForMode("act"),
    ...session.turns.flatMap((turn) => {
      const activity = turn.activity_json.map((item) => item.label);
      const result = turn.assistant_message
        ?? turn.error_message
        ?? (turn.status === "queued" ? "Queued for Act." : "Act is working...");
      return [
        { id: turn.id * 10 + 2, role: "user" as const, content: turn.user_message, actTurnId: turn.id },
        {
          id: turn.id * 10 + 3,
          role: "assistant" as const,
          content: [...activity, result].filter((value, index, values) => value && values.indexOf(value) === index).join("\n\n"),
          kind: isActiveActTurn(turn) ? "thinking" as const : "text" as const,
          actTurnId: turn.id,
        },
      ];
    }),
  ];
}
