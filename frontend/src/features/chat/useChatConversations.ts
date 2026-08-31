import { useEffect, useMemo, useState } from "react";

import { ActSessionSummary, ConversationMode } from "../../api/client";
import {
  appendMessages as appendStoredMessages,
  CHAT_STORAGE_KEY,
  CHAT_UPDATED_EVENT,
  ChatConversation,
  ChatMessage,
  createConversation,
  initialMessagesForMode,
  loadActiveConversationId,
  loadStoredConversations,
  saveActiveConversationId,
  saveStoredConversations,
  updateStoredConversations,
} from "../../lib/chatStore";

export function useChatConversations() {
  const [conversations, setConversations] = useState<ChatConversation[]>(() => loadStoredConversations());
  const [activeConversationId, setActiveConversationId] = useState(() => loadActiveConversationId(conversations));
  const activeConversation = useMemo(
    () => conversations.find((conversation) => conversation.id === activeConversationId),
    [activeConversationId, conversations],
  );
  const messages = activeConversation?.messages ?? initialMessagesForMode("project");
  const draft = activeConversation?.draft ?? "";
  const mode = activeConversation?.mode ?? "project";

  useEffect(() => {
    // The initial load may synthesize a project after removing unsupported
    // legacy direct-chat rows; persist that exact normalized collection.
    saveStoredConversations(conversations);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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

  function updateConversation(
    conversationId: string,
    updater: (conversation: ChatConversation) => ChatConversation,
  ) {
    const nextConversations = updateStoredConversations((current) =>
      current.map((conversation) => (conversation.id === conversationId ? updater(conversation) : conversation)),
    );
    setConversations(nextConversations);
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
    mode: ConversationMode = "project",
    options: { actSessionId?: number; title?: string } = {},
  ): string {
    const conversation = createConversation(mode, options);
    const nextConversations = updateStoredConversations((current) => [conversation, ...current]);
    setConversations(nextConversations);
    selectConversation(conversation.id);
    return conversation.id;
  }

  function selectConversation(conversationId: string) {
    saveActiveConversationId(conversationId);
    setActiveConversationId(conversationId);
  }

  function deleteConversation(conversationId: string): ChatConversation[] {
    const nextConversations = updateStoredConversations((current) => {
      const remaining = current.filter((conversation) => conversation.id !== conversationId);
      return remaining.length ? remaining : [createConversation()];
    });
    setConversations(nextConversations);
    if (
      conversationId === activeConversationId ||
      !nextConversations.some((conversation) => conversation.id === activeConversationId)
    ) {
      selectConversation(nextConversations[0].id);
    }
    return nextConversations;
  }

  function updateDraft(value: string) {
    updateActiveConversation((conversation) => ({
      ...conversation,
      draft: value,
      updatedAt: new Date().toISOString(),
    }));
  }

  function importActSessions(sessions: ActSessionSummary[]) {
    const nextConversations = updateStoredConversations((current) => {
      const knownSessionIds = new Set(
        current
          .map((conversation) => conversation.actSessionId)
          .filter((sessionId): sessionId is number => typeof sessionId === "number"),
      );
      const imported = sessions
        .filter((session) => !knownSessionIds.has(session.id))
        .map((session) => createConversation("act", {
          id: `act-${session.id}`,
          title: session.title,
          actSessionId: session.id,
          createdAt: session.created_at,
          updatedAt: session.updated_at,
        }));
      return [...imported, ...current].map((conversation) => {
        if (conversation.mode !== "act" || conversation.actSessionId === undefined) return conversation;
        const session = sessions.find((item) => item.id === conversation.actSessionId);
        return session ? { ...conversation, title: session.title, updatedAt: session.updated_at } : conversation;
      });
    });
    setConversations(nextConversations);
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
    conversations,
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
    selectConversation,
    deleteConversation,
    updateDraft,
    updateMessageInConversation,
    removeMessageFromConversation,
  };
}
