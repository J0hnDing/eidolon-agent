import { FormEvent, useEffect, useRef, useState } from "react";

import { ActSession, ActTurn, AgentId, ConversationMode, api } from "../api/client";
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
  const pendingActSessions = useRef(new Map<string, Promise<ActSession>>());
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

  async function syncActConversation(conversationId: string, sessionId: number, agentId: AgentId): Promise<boolean> {
    try {
      const session = await (agentId === "act" ? api.getActSession(sessionId) : api.getAgentSession(agentId, sessionId));
      const activeTurn = [...session.turns].reverse().find(isActiveActTurn) ?? null;
      setActStateNeedsPolling(Boolean(activeTurn));
      if (conversationId === chat.activeConversationId) setActiveActTurnId(activeTurn?.id ?? null);
      chat.updateConversation(conversationId, (conversation) => ({
        ...conversation,
        title: session.title,
        messages: actSessionMessages(session, agentId),
        updatedAt: session.updated_at,
      }));
      return true;
    } catch {
      return false;
    }
  }

  useEffect(() => {
    for (const agentId of ["act", "observer", "assistant"] as const) {
      const request = agentId === "act" ? api.listActSessions() : api.listAgentSessions(agentId);
      void request.then((sessions) => chat.importAgentSessions(agentId, sessions))
        .catch((reason) => setError(reason instanceof Error ? reason.message : `Could not load ${agentId} conversations`));
    }
    // Active agent sessions are imported once when the shared conversation page mounts.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    setActiveActTurnId(null);
    setActStateNeedsPolling(false);
    const sessionId = chat.activeConversation?.actSessionId;
    if (chat.mode !== "project" && sessionId !== undefined) {
      void syncActConversation(chat.activeConversationId, sessionId, chat.mode);
    }
    // Agent synchronization is keyed only by the active local conversation binding.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chat.activeConversationId, chat.mode, chat.activeConversation?.actSessionId]);

  usePolling(
    async () => {
      const sessionId = chat.activeConversation?.actSessionId;
      if (chat.mode !== "project" && sessionId !== undefined) await syncActConversation(chat.activeConversationId, sessionId, chat.mode);
    },
    chat.mode !== "project" && actStateNeedsPolling,
    1000,
  );

  async function handleNewConversation(mode: ConversationMode) {
    setError(null);
    if (mode === "project") {
      chat.createNewConversation(mode);
      return;
    }
    const conversationId = chat.createNewConversation(mode);
    const sessionPromise = mode === "act" ? api.createActSession() : api.createAgentSession(mode);
    pendingActSessions.current.set(conversationId, sessionPromise);
    try {
      const session = await sessionPromise;
      chat.updateConversation(conversationId, (conversation) => ({
        ...conversation,
        actSessionId: session.id,
        title: session.title,
        updatedAt: session.updated_at,
      }));
    } catch (reason) {
      chat.deleteConversation(conversationId);
      setError(reason instanceof Error ? reason.message : "Could not create conversation");
    } finally {
      pendingActSessions.current.delete(conversationId);
    }
  }

  async function handleDeleteConversation(conversationId: string) {
    const conversation = chat.conversations.find((item) => item.id === conversationId);
    if (!conversation) return;
    setError(null);
    if (conversation.mode !== "project" && conversation.actSessionId !== undefined) {
      try {
        if (conversation.mode === "act") await api.archiveActSession(conversation.actSessionId);
        else await api.archiveAgentSession(conversation.mode, conversation.actSessionId);
        chat.deleteConversation(conversationId);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not archive conversation");
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
    let actSessionId = chat.activeConversation?.actSessionId;
    const nextId = Date.now();
    const thinkingId = nextId + 1;
    chat.appendMessagesToConversation(conversationId, [
      { id: nextId, role: "user", content },
      ...(mode === "project" ? [{
        id: thinkingId,
        role: "assistant" as const,
        content: "ProductManager is reviewing the project...",
        kind: "thinking" as const,
      }] : []),
    ]);
    chat.updateConversation(conversationId, (conversation) => ({ ...conversation, draft: "" }));
    setIsSending(true);
    setError(null);
    try {
      if (mode !== "project") {
        if (actSessionId === undefined) {
          actSessionId = (await pendingActSessions.current.get(conversationId))?.id;
        }
        if (actSessionId === undefined) throw new Error("This conversation is not connected to a session.");
        if (mode === "act") await api.runActTurn(actSessionId, content);
        else await api.runAgentTurn(mode, actSessionId, content);
        setActStateNeedsPolling(true);
        await syncActConversation(conversationId, actSessionId, mode);
        return;
      }
      const response = await api.sendProjectMessage(
        content,
        chat.activeConversation?.pendingGenerationRequestId,
        conversationId,
      );
      chat.removeMessageFromConversation(conversationId, thinkingId);
      if (response.type === "project_not_plausible") {
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
      if (mode !== "project" && actSessionId !== undefined && await syncActConversation(conversationId, actSessionId, mode)) {
        setError(null);
        return;
      }
      if (mode === "project" && await syncProjectConversation(conversationId)) {
        setError(null);
        return;
      }
      const message = err instanceof Error ? err.message : "Could not send conversation message";
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
    if (chat.mode === "project" || sessionId === undefined || activeActTurnId === null) return;
    setError(null);
    try {
      if (chat.mode === "act") await api.cancelActTurn(sessionId, activeActTurnId);
      else await api.cancelAgentTurn(chat.mode, sessionId, activeActTurnId);
      await syncActConversation(chat.activeConversationId, sessionId, chat.mode);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not cancel the turn");
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

function actSessionMessages(session: ActSession, agentId: AgentId): ChatMessage[] {
  return [
    ...initialMessagesForMode(agentId),
    ...session.turns.flatMap((turn) => {
      const active = isActiveActTurn(turn);
      const result = active
        ? "Eidolon is thinking..."
        : turn.assistant_message ?? turn.error_message ?? `${agentId} did not return a response.`;
      return [
        { id: turn.id * 10 + 2, role: "user" as const, content: turn.user_message, actTurnId: turn.id },
        {
          id: turn.id * 10 + 3,
          role: "assistant" as const,
          content: result,
          kind: active ? "thinking" as const : "text" as const,
          actTurnId: turn.id,
          actWork: {
            status: turn.status,
            activities: turn.activity_json,
            startedAt: turn.started_at ?? turn.created_at,
            completedAt: turn.completed_at,
          },
        },
      ];
    }),
  ];
}
