import { FormEvent, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { ActSession, ActTurn, AgentId, ConversationMode, api } from "../api/client";
import {
  ChatWorkspace,
  buildApprovalMessage,
  generationResultMessage,
  runtimeApprovalMessage,
} from "../features/chat/ChatWorkspace";
import { useChatConversations } from "../features/chat/useChatConversations";
import { mergeProjectConversationState } from "../features/chat/projectConversationState";
import { isActiveActTurn } from "../features/chat/actConversationState";
import { cacheSession, getCachedSession } from "../features/chat/managedSessionCache";
import { useAwaitingActReply } from "../features/chat/useAwaitingActReply";
import { ChatMessage } from "../lib/chatStore";
import { usePolling } from "../lib/usePolling";
import { Modal } from "../components/Modal";

export default function ChatPage({ surface, agentId = "act" }: { surface?: "assistant" | "agent" | "build"; agentId?: "act" | "observer" }) {
  const scope = surface === "assistant" ? "assistant" : surface === "build" ? "project" : surface === "agent" ? agentId : undefined;
  const chat = useChatConversations(scope);
  const cachedHistory = chat.mode !== "project" && chat.activeConversation?.actSessionId !== undefined
    ? getCachedSession(chat.mode, chat.activeConversation.actSessionId) : undefined;
  const cachedActiveTurn = (cachedHistory ? [...cachedHistory.session.turns].reverse().find(isActiveActTurn) : undefined) ?? null;
  const [searchParams, setSearchParams] = useSearchParams();
  const selectedConversationId = useRef(chat.activeConversationId);
  selectedConversationId.current = chat.activeConversationId;
  const [isSending, setIsSending] = useState(false);
  const [isGenerating, setIsGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [projectStateNeedsPolling, setProjectStateNeedsPolling] = useState(false);
  const [actStateNeedsPolling, setActStateNeedsPolling] = useState(Boolean(cachedActiveTurn));
  const [activeActTurnId, setActiveActTurnId] = useState<number | null>(cachedActiveTurn?.id ?? null);
  const [activeActTurn, setActiveActTurn] = useState<ActTurn | null>(cachedActiveTurn);
  const [submittedReplyId, setSubmittedReplyId] = useState<number | null>(null);
  const submissionPending = useRef(false);
  const pendingActSessions = useRef(new Map<string, Promise<ActSession>>());
  const syncRevisions = useRef(new Map<string, number>());
  const [sessionDiscovery, setSessionDiscovery] = useState<"loading" | "ready" | "error">(scope === "project" ? "ready" : "loading");
  const [discoveryRevision, setDiscoveryRevision] = useState(0);
  const [historyStates, setHistoryStates] = useState<Record<string, "ready" | "error">>({});
  const historyKey = `${chat.activeConversationId}:${chat.activeConversation?.actSessionId ?? "new"}`;
  const needsHistory = chat.mode !== "project" && chat.activeConversation?.actSessionId !== undefined;
  const historyError = !cachedHistory && (sessionDiscovery === "error" || (needsHistory && historyStates[historyKey] === "error"));
  const historyLoading = !cachedHistory && !historyError && (sessionDiscovery === "loading" || (needsHistory && !historyStates[historyKey]));
  useEffect(() => () => { syncRevisions.current.clear(); }, []);
  const hasPendingMessage = chat.messages.some(
    (message) => message.kind === "thinking" || message.actionStatus === "working",
  );
  const isBusy = isSending || isGenerating || hasPendingMessage || activeActTurnId !== null;
  const awaitingReply = useAwaitingActReply(
    (chat.mode === "act" || chat.mode === "assistant") && activeActTurn?.session_id === chat.activeConversation?.actSessionId ? activeActTurn : null,
  );
  const canReplyToAct = awaitingReply !== null && awaitingReply.id !== submittedReplyId;

  async function syncProjectConversation(conversationId: string): Promise<boolean> {
    try {
      const state = await api.getProjectConversationState(conversationId);
      if (selectedConversationId.current === conversationId) setProjectStateNeedsPolling(Boolean(state?.needs_polling));
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

  async function syncActConversation(conversationId: string, sessionId: number, agentId: AgentId, expectedTurn?: { afterId: number; message: string }): Promise<boolean> {
    const revision = (syncRevisions.current.get(conversationId) ?? 0) + 1;
    syncRevisions.current.set(conversationId, revision);
    try {
      const session = await (agentId === "act" ? api.getActSession(sessionId) : api.getAgentSession(agentId, sessionId));
      if (syncRevisions.current.get(conversationId) !== revision) return false;
      const activeTurn = [...session.turns].reverse().find(isActiveActTurn) ?? null;
      if (conversationId === selectedConversationId.current) {
        setActStateNeedsPolling(Boolean(activeTurn));
        setActiveActTurnId(activeTurn?.id ?? null);
        setActiveActTurn(activeTurn);
      }
      chat.updateConversation(conversationId, (conversation) => ({
        ...conversation,
        title: session.title,
        origin: session.origin,
        wecomUserId: session.wecom_user_id ?? undefined,
        createdAt: session.created_at,
        messages: cacheSession(agentId, session).messages,
        updatedAt: session.updated_at,
      }));
      setHistoryStates((current) => ({ ...current, [`${conversationId}:${sessionId}`]: "ready" }));
      return !expectedTurn || session.turns.some((turn) => turn.id > expectedTurn.afterId && turn.user_message === expectedTurn.message);
    } catch {
      if (syncRevisions.current.get(conversationId) === revision) {
        setHistoryStates((current) => current[`${conversationId}:${sessionId}`] === "ready" ? current : { ...current, [`${conversationId}:${sessionId}`]: "error" });
      }
      return false;
    }
  }

  useEffect(() => {
    let cancelled = false;
    const importedAgentIds: AgentId[] = scope === "project" ? [] : scope ? [scope] : ["act", "observer", "assistant"];
    let remaining = importedAgentIds.length;
    let failed = false;
    for (const agentId of importedAgentIds) {
      const request = agentId === "act" ? api.listActSessions() : api.listAgentSessions(agentId);
      void request.then(async (sessions) => {
        if (cancelled) return;
        const initial = chat.activeConversation;
        if (agentId === "assistant" && sessions.length) {
          const runtime = await api.getAssistantRuntime().catch(() => null);
          if (cancelled) return;
          const primary = sessions.find((session) => session.id === runtime?.primary_session_id) ?? sessions[0];
          if (initial?.mode === "assistant" && initial.actSessionId === undefined) {
            chat.updateConversation(initial.id, (conversation) => ({
              ...conversation, actSessionId: primary.id, title: primary.title,
              origin: primary.origin, createdAt: primary.created_at, updatedAt: primary.updated_at,
            }));
          }
          chat.importAgentSessions(agentId, sessions);
          if (scope === "assistant") {
            const existing = chat.conversations.find((conversation) => conversation.actSessionId === primary.id)
              ?? (initial?.actSessionId === undefined ? initial : undefined);
            chat.selectConversation(existing?.id ?? `assistant-${primary.id}`);
          }
          return;
        }
        chat.importAgentSessions(agentId, sessions);
      })
        .catch((reason) => { if (!cancelled) { failed = true; setError(reason instanceof Error ? reason.message : `Could not load ${agentId} conversations`); } })
        .finally(() => { if (!cancelled && --remaining === 0) setSessionDiscovery(failed ? "error" : "ready"); });
    }
    return () => { cancelled = true; };
    // Each route owns one stable conversation scope; failed discovery can be retried.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [discoveryRevision]);

  useEffect(() => {
    if (surface !== "agent") return;
    const requested = searchParams.get("session");
    if (!requested) return;
    const conversation = chat.conversations.find((item) => String(item.actSessionId) === requested);
    if (conversation && conversation.id !== chat.activeConversationId) chat.selectConversation(conversation.id);
    // Session links may resolve after the backend session list has arrived.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams, chat.conversations, surface]);

  useEffect(() => {
    setActiveActTurnId(cachedActiveTurn?.id ?? null);
    setActiveActTurn(cachedActiveTurn);
    setActStateNeedsPolling(Boolean(cachedActiveTurn));
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
    chat.mode !== "project" && (actStateNeedsPolling || chat.mode === "assistant"),
    actStateNeedsPolling ? 1000 : 5000,
  );

  async function handleNewConversation(mode: ConversationMode) {
    setError(null);
    if (surface === "agent") setSearchParams({}, { replace: true });
    if (mode === "project") {
      chat.createNewConversation(mode);
      return;
    }
    if (mode === "assistant") {
      try {
        const [runtime, sessions] = await Promise.all([api.getAssistantRuntime(), api.listAgentSessions("assistant")]);
        const primary = sessions.find((session) => session.id === runtime.primary_session_id) ?? sessions[0];
        if (primary) {
          const existing = chat.conversations.find((conversation) => conversation.mode === "assistant" && conversation.actSessionId === primary.id);
          chat.importAgentSessions("assistant", sessions);
          chat.selectConversation(existing?.id ?? `assistant-${primary.id}`);
          return;
        }
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "Could not open Assistant conversation");
        return;
      }
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
        origin: session.origin,
        wecomUserId: session.wecom_user_id ?? undefined,
        createdAt: session.created_at,
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
        if (conversation.mode !== "act" && conversation.wecomUserId) {
          const sessions = await api.listAgentSessions(conversation.mode);
          chat.importAgentSessions(conversation.mode, sessions);
          const replacement = sessions.find(
            (session) => session.wecom_user_id === conversation.wecomUserId,
          );
          if (replacement) chat.selectConversation(`${conversation.mode}-${replacement.id}`);
        }
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
    if (!content || historyLoading || historyError || submissionPending.current || isSending || isGenerating || ((hasPendingMessage || activeActTurnId !== null) && !canReplyToAct)) return;
    submissionPending.current = true;
    const replyingToId = canReplyToAct ? awaitingReply?.id ?? null : null;
    if (replyingToId !== null) setSubmittedReplyId(replyingToId);

    const conversationId = chat.activeConversationId;
    const mode = chat.mode;
    let actSessionId = chat.activeConversation?.actSessionId;
    const nextId = Date.now();
    const thinkingId = nextId + 1;
    const lastTurnId = Math.max(0, ...chat.messages.map((message) => message.actTurnId ?? 0));
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
        if (actSessionId === undefined && surface) {
          const session = await (mode === "act" ? api.createActSession() : api.createAgentSession(mode));
          actSessionId = session.id;
          chat.updateConversation(conversationId, (conversation) => ({ ...conversation, actSessionId: session.id, title: session.title, createdAt: session.created_at, updatedAt: session.updated_at }));
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
      if (replyingToId !== null) setSubmittedReplyId(null);
      chat.removeMessageFromConversation(conversationId, thinkingId);
      if (mode !== "project" && actSessionId !== undefined && await syncActConversation(conversationId, actSessionId, mode, { afterId: lastTurnId, message: content })) {
        setError(null);
        return;
      }
      if (mode === "project" && await syncProjectConversation(conversationId)) {
        setError(null);
        return;
      }
      const message = err instanceof Error ? err.message : "Could not send conversation message";
      chat.updateConversation(conversationId, (conversation) => ({ ...conversation, draft: conversation.draft || content }));
      chat.appendMessagesToConversation(conversationId, [
        { id: nextId + 3, role: "assistant", content: message },
      ]);
      setError(message);
    } finally {
      submissionPending.current = false;
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
    <><ChatWorkspace
      embedded={Boolean(surface)}
      hideSidebar={surface === "assistant"}
      title={surface === "assistant" ? "Assistant" : surface === "build" ? "Skill build" : surface === "agent" ? (agentId === "act" ? "Act" : "Observer") : "Chat"}
      allowedModes={scope ? [scope] : undefined}
      conversations={chat.conversations}
      activeConversationId={chat.activeConversationId}
      messages={chat.messages}
      draft={chat.draft}
      mode={chat.mode}
      isBusy={isBusy}
      historyLoading={historyLoading}
      historyError={historyError}
      onRetryHistory={() => {
        setError(null);
        if (sessionDiscovery === "error") { setSessionDiscovery("loading"); setDiscoveryRevision((value) => value + 1); }
        const sessionId = chat.activeConversation?.actSessionId;
        if (chat.mode !== "project" && sessionId !== undefined) {
          setHistoryStates((current) => { const next = { ...current }; delete next[historyKey]; return next; });
          void syncActConversation(chat.activeConversationId, sessionId, chat.mode);
        }
      }}
      canReplyToAct={canReplyToAct}
      isSending={isSending}
      isGenerating={isGenerating}
      error={error}
      onNewConversation={handleNewConversation}
      onSelectConversation={(id) => {
        chat.selectConversation(id);
        const sessionId = chat.conversations.find((conversation) => conversation.id === id)?.actSessionId;
        if (surface === "agent") setSearchParams(sessionId ? { session: String(sessionId) } : {}, { replace: true });
      }}
      onDeleteConversation={(id) => surface ? setDeletingId(id) : void handleDeleteConversation(id)}
      activeActTurnId={activeActTurnId}
      onCancelAct={handleCancelAct}
      onDraftChange={chat.updateDraft}
      onSubmit={handleSubmit}
      onApproveBuild={handleApprove}
      onDenyBuild={handleDeny}
      onApproveRuntime={handleApproveRuntime}
      onDenyRuntime={handleDenyRuntime}
    />
      {deletingId && <Modal title={scope === "project" ? "Delete this build conversation?" : "Archive this conversation?"} onClose={() => setDeletingId(null)}>
        <p>{scope === "project" ? "This removes this build conversation and its saved messages. Skills and approval records remain available." : "This conversation will leave the active list. Its retained history remains available in Memory."}</p>
        <div className="button-row"><button type="button" className="danger" onClick={async () => { const id = deletingId; setDeletingId(null); await handleDeleteConversation(id); }}>{scope === "project" ? "Delete conversation" : "Archive conversation"}</button><button className="secondary" type="button" onClick={() => setDeletingId(null)}>Cancel</button></div>
      </Modal>}
    </>
  );
}
