import {
  AgentRun,
  ApprovalRequest,
  ChatMode,
  ProposedSkillValidation,
  Skill,
  SkillGenerationRequest,
} from "../api/client";

export type ChatMessage = {
  id: number;
  role: "user" | "assistant";
  content: string;
  kind?: "text" | "thinking" | "build_approval" | "runtime_approval" | "generation_result";
  generationRequest?: SkillGenerationRequest;
  permissionRequest?: ApprovalRequest;
  skill?: Skill;
  validation?: ProposedSkillValidation | null;
  agentRun?: AgentRun | null;
  actionStatus?: "pending" | "working" | "approved" | "denied" | "expired" | "superseded" | "completed" | "failed";
};

export const initialMessages: ChatMessage[] = [
  {
    id: 1,
    role: "assistant",
    content: "Use Chat mode for normal Codex-backed conversation. Switch to Project mode when you want me to propose a reusable skill.",
  },
];

export type ChatConversation = {
  id: string;
  title: string;
  mode: ChatMode;
  draft: string;
  messages: ChatMessage[];
  createdAt: string;
  updatedAt: string;
};

export const CHAT_STORAGE_KEY = "personal-agent.chat-conversations.v1";
export const ACTIVE_CONVERSATION_KEY = "personal-agent.active-chat-conversation.v1";
export const CHAT_UPDATED_EVENT = "personal-agent-chat-updated";

export function createConversation(): ChatConversation {
  const now = new Date().toISOString();
  return {
    id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
    title: "New chat",
    mode: "chat",
    draft: "",
    messages: initialMessages,
    createdAt: now,
    updatedAt: now,
  };
}

export function saveStoredConversations(conversations: ChatConversation[]) {
  localStorage.setItem(CHAT_STORAGE_KEY, JSON.stringify(conversations));
  window.dispatchEvent(new Event(CHAT_UPDATED_EVENT));
}

export function updateStoredConversations(
  updater: (current: ChatConversation[]) => ChatConversation[],
): ChatConversation[] {
  const next = updater(loadStoredConversations());
  saveStoredConversations(next);
  return next;
}

export function loadActiveConversationId(conversations: ChatConversation[]): string {
  const stored = localStorage.getItem(ACTIVE_CONVERSATION_KEY);
  if (stored && conversations.some((conversation) => conversation.id === stored)) {
    return stored;
  }
  return conversations[0]?.id ?? createConversation().id;
}

export function saveActiveConversationId(conversationId: string) {
  localStorage.setItem(ACTIVE_CONVERSATION_KEY, conversationId);
}

export function appendMessagesToActiveConversation(newMessages: ChatMessage[]) {
  updateStoredConversations((current) => {
    const activeId = loadActiveConversationId(current);
    return current.map((conversation) =>
      conversation.id === activeId ? appendMessages(conversation, newMessages) : conversation,
    );
  });
}

export function replaceActiveConversationMessage(messageId: number, replacementMessages: ChatMessage[]) {
  updateStoredConversations((current) => {
    const activeId = loadActiveConversationId(current);
    return current.map((conversation) => {
      if (conversation.id !== activeId) return conversation;
      return {
        ...conversation,
        messages: conversation.messages.flatMap((message) =>
          message.id === messageId ? replacementMessages : [message],
        ),
        updatedAt: new Date().toISOString(),
      };
    });
  });
}

export function loadStoredConversations(): ChatConversation[] {
  try {
    const raw = localStorage.getItem(CHAT_STORAGE_KEY);
    if (!raw) return [createConversation()];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [createConversation()];
    const conversations = parsed.filter(isStoredConversation).map(normalizeStoredConversation);
    return conversations.length ? conversations : [createConversation()];
  } catch {
    return [createConversation()];
  }
}

export function appendMessages(conversation: ChatConversation, newMessages: ChatMessage[]): ChatConversation {
  const firstUserMessage = newMessages.find((message) => message.role === "user")?.content;
  return {
    ...conversation,
    title: conversation.title === "New chat" && firstUserMessage ? makeTitle(firstUserMessage) : conversation.title,
    messages: [...conversation.messages, ...newMessages],
    updatedAt: new Date().toISOString(),
  };
}

function isStoredConversation(value: unknown): value is ChatConversation {
  if (!value || typeof value !== "object") return false;
  const raw = value as Record<string, unknown>;
  return (
    typeof raw.id === "string" &&
    typeof raw.title === "string" &&
    Array.isArray(raw.messages) &&
    typeof raw.createdAt === "string" &&
    typeof raw.updatedAt === "string"
  );
}

function normalizeStoredConversation(conversation: ChatConversation): ChatConversation {
  return {
    ...conversation,
    mode: conversation.mode === "project" ? "project" : "chat",
    draft: typeof conversation.draft === "string" ? conversation.draft : "",
  };
}

function makeTitle(content: string): string {
  const normalized = content.replace(/\s+/g, " ").trim();
  if (normalized.length <= 42) return normalized || "New chat";
  return `${normalized.slice(0, 39)}...`;
}
