import { useEffect, useMemo, useRef, useState } from "react";

import { ActSessionSummary, AgentId, ConversationMode } from "../../api/client";
import { forgetCachedSession, getCachedSession, reconcileCachedSessions } from "./managedSessionCache";
import {
  appendMessages as appendStoredMessages,
  CHAT_STORAGE_KEY,
  ACTIVE_CONVERSATION_KEY,
  CHAT_UPDATED_EVENT,
  ChatConversation,
  ChatMessage,
  createConversation,
  initialMessagesForMode,
  loadActiveConversationId,
  loadStoredConversations,
  saveActiveConversationId,
  saveStoredConversations,
} from "../../lib/chatStore";

// Unsaved agent drafts survive navigation within this browser session. Managed
// transcript content and drafts are never written to localStorage.
const managedDrafts = new Map<string, string>();

function restoreCachedHistory(conversation: ChatConversation): ChatConversation {
  if (conversation.mode === "project" || conversation.actSessionId === undefined) return conversation;
  const cached = getCachedSession(conversation.mode, conversation.actSessionId);
  return cached ? { ...conversation, messages: cached.messages } : conversation;
}

export function useChatConversations(scope?: ConversationMode) {
  const selectionKey = scope ? `${ACTIVE_CONVERSATION_KEY}.${scope}` : ACTIVE_CONVERSATION_KEY;
  const [conversations, setConversations] = useState<ChatConversation[]>(() => {
    const stored = loadStoredConversations().map(restoreCachedHistory).map((conversation) => ({
      ...conversation, draft: managedDrafts.get(conversation.id) ?? conversation.draft,
    }));
    return scope && !stored.some((conversation) => conversation.mode === scope)
      ? [...stored, createConversation(scope)] : stored;
  });
  const conversationsRef = useRef(conversations);
  const visibleConversations = scope ? conversations.filter((conversation) => conversation.mode === scope) : conversations;
  const [activeConversationId, setActiveConversationId] = useState(() => {
    const selected = localStorage.getItem(selectionKey);
    return visibleConversations.find((conversation) => conversation.id === selected)?.id
      ?? loadActiveConversationId(visibleConversations);
  });
  const activeConversation = useMemo(
    () => conversations.find((conversation) => conversation.id === activeConversationId && (!scope || conversation.mode === scope)),
    [activeConversationId, conversations, scope],
  );
  const messages = activeConversation?.messages ?? initialMessagesForMode(scope ?? "assistant");
  const draft = activeConversation?.draft ?? "";
  const mode = activeConversation?.mode ?? scope ?? "assistant";

  useEffect(() => {
    // The initial load may synthesize an Assistant chat after removing unsupported
    // legacy direct-chat rows; persist that exact normalized collection.
    saveStoredConversations(conversations);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (visibleConversations.length === 0) {
      const conversation = createConversation(scope);
      const next = [...conversationsRef.current, conversation];
      conversationsRef.current = next;
      saveStoredConversations(next);
      setConversations(next);
      selectConversation(conversation.id);
    }
  }, [visibleConversations.length, scope]);

  useEffect(() => {
    function handleStoredChatUpdate() {
      const current = new Map(conversationsRef.current.map((conversation) => [conversation.id, conversation]));
      const next = loadStoredConversations().map((stored) => {
        const existing = current.get(stored.id);
        if (stored.mode === "project" || !existing || existing.mode !== stored.mode) return restoreCachedHistory(stored);
        // Managed-agent transcripts live in the current view or server archive,
        // never in localStorage. Keep the current view while metadata changes.
        return { ...stored, draft: existing.draft, messages: existing.messages };
      });
      conversationsRef.current = next;
      setConversations(next);
    }
    window.addEventListener(CHAT_UPDATED_EVENT, handleStoredChatUpdate);
    return () => window.removeEventListener(CHAT_UPDATED_EVENT, handleStoredChatUpdate);
  }, []);

  function updateConversationList(updater: (current: ChatConversation[]) => ChatConversation[]): ChatConversation[] {
    const next = updater(conversationsRef.current);
    conversationsRef.current = next;
    saveStoredConversations(next);
    setConversations(next);
    return next;
  }

  function updateConversation(
    conversationId: string,
    updater: (conversation: ChatConversation) => ChatConversation,
  ) {
    updateConversationList((current) =>
      current.map((conversation) => {
        if (conversation.id !== conversationId) return conversation;
        const updated = updater(conversation);
        if (updated.mode !== "project") managedDrafts.set(updated.id, updated.draft);
        return updated;
      }),
    );
  }

  function updateActiveConversation(updater: (conversation: ChatConversation) => ChatConversation) {
    updateConversation(activeConversationId, updater);
  }

  function appendMessagesToConversation(conversationId: string, newMessages: ChatMessage[]) {
    updateConversation(conversationId, (conversation) => appendStoredMessages(conversation, newMessages));
  }

  function appendMessages(newMessages: ChatMessage[]) {
    appendMessagesToConversation(activeConversationId, newMessages);
  }

  function createNewConversation(
    mode: ConversationMode = "assistant",
    options: { actSessionId?: number; title?: string } = {},
  ): string {
    const conversation = createConversation(mode, options);
    updateConversationList((current) => [conversation, ...current]);
    selectConversation(conversation.id);
    return conversation.id;
  }

  function selectConversation(conversationId: string) {
    saveActiveConversationId(conversationId);
    localStorage.setItem(selectionKey, conversationId);
    setActiveConversationId(conversationId);
  }

  function deleteConversation(conversationId: string): ChatConversation[] {
    const removed = conversationsRef.current.find((conversation) => conversation.id === conversationId);
    if (removed && removed.mode !== "project" && removed.actSessionId !== undefined) forgetCachedSession(removed.mode, removed.actSessionId);
    managedDrafts.delete(conversationId);
    const nextConversations = updateConversationList((current) => {
      const remaining = current.filter((conversation) => conversation.id !== conversationId);
      return remaining.some((conversation) => !scope || conversation.mode === scope)
        ? remaining : [...remaining, createConversation(scope)];
    });
    if (
      conversationId === activeConversationId ||
      !nextConversations.some((conversation) => conversation.id === activeConversationId)
    ) {
      selectConversation(nextConversations.find((conversation) => !scope || conversation.mode === scope)!.id);
    }
    return nextConversations;
  }

  function updateDraft(value: string) {
    if (mode !== "project") managedDrafts.set(activeConversationId, value);
    updateActiveConversation((conversation) => ({
      ...conversation,
      draft: value,
      updatedAt: new Date().toISOString(),
    }));
  }

  function importActSessions(sessions: ActSessionSummary[]) {
    importAgentSessions("act", sessions);
  }

  function importAgentSessions(agentId: AgentId, sessions: ActSessionSummary[]) {
    reconcileCachedSessions(agentId, sessions.map((session) => session.id));
    const nextConversations = updateConversationList((current) => {
      // Reconcile archived/pruned sessions as well as sessions created in Agents or Telegram.
      const retained = current.filter((conversation) =>
        conversation.mode !== agentId || conversation.actSessionId === undefined ||
        sessions.some((session) => session.id === conversation.actSessionId),
      );
      const knownSessionIds = new Set(retained.filter((item) => item.mode === agentId)
        .map((item) => item.actSessionId));
      const imported = sessions.filter((session) => !knownSessionIds.has(session.id))
        .map((session) => restoreCachedHistory(createConversation(agentId, {
          id: `${agentId}-${session.id}`,
          title: session.title,
          actSessionId: session.id,
          origin: session.origin,
          wecomUserId: session.wecom_user_id ?? undefined,
          createdAt: session.created_at,
          updatedAt: session.updated_at,
        })));
      return [...imported, ...retained].map((conversation) => {
        if (conversation.mode !== agentId || conversation.actSessionId === undefined) return conversation;
        const session = sessions.find((item) => item.id === conversation.actSessionId);
        return session ? {
          ...conversation,
          title: session.title,
          origin: session.origin,
          wecomUserId: session.wecom_user_id ?? undefined,
          createdAt: session.created_at,
          updatedAt: session.updated_at,
        } : conversation;
      });
    });
    setActiveConversationId((selectedId) => {
      if (nextConversations.some((item) => item.id === selectedId && (!scope || item.mode === scope))) return selectedId;
      const next = nextConversations.find((item) => !scope || item.mode === scope);
      if (!next) return selectedId;
      saveActiveConversationId(next.id);
      localStorage.setItem(selectionKey, next.id);
      return next.id;
    });
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

  function removeMessageFromConversation(conversationId: string, messageId: number) {
    updateConversation(conversationId, (conversation) => ({
      ...conversation,
      messages: conversation.messages.filter((message) => message.id !== messageId),
      updatedAt: new Date().toISOString(),
    }));
  }

  return {
    conversations: visibleConversations,
    activeConversationId,
    activeConversation,
    messages,
    draft,
    mode,
    updateConversation,
    appendMessages,
    appendMessagesToConversation,
    createNewConversation,
    importActSessions,
    importAgentSessions,
    selectConversation,
    deleteConversation,
    updateDraft,
    updateMessageInConversation,
    removeMessageFromConversation,
  };
}
