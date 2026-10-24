import { ActSession, AgentId } from "../../api/client";
import { ChatMessage } from "../../lib/chatStore";
import { actSessionMessages } from "./actConversationState";

type CachedSession = { session: ActSession; messages: ChatMessage[] };

// Navigation cache only: no transcripts are written to browser storage.
const sessions = new Map<string, CachedSession>();
const maxSessions = 20;
const key = (agentId: AgentId, sessionId: number) => `${agentId}:${sessionId}`;

export function getCachedSession(agentId: AgentId, sessionId: number): CachedSession | undefined {
  return sessions.get(key(agentId, sessionId));
}

export function cacheSession(agentId: AgentId, session: ActSession): CachedSession {
  const entry = { session, messages: actSessionMessages(session, agentId) };
  const sessionKey = key(agentId, session.id);
  sessions.delete(sessionKey);
  sessions.set(sessionKey, entry);
  if (sessions.size > maxSessions) sessions.delete(sessions.keys().next().value!);
  return entry;
}

export function forgetCachedSession(agentId: AgentId, sessionId: number) {
  sessions.delete(key(agentId, sessionId));
}

export function reconcileCachedSessions(agentId: AgentId, activeIds: number[]) {
  const retained = new Set(activeIds.map((id) => key(agentId, id)));
  for (const sessionKey of sessions.keys()) {
    if (sessionKey.startsWith(`${agentId}:`) && !retained.has(sessionKey)) sessions.delete(sessionKey);
  }
}

export function clearManagedSessionCache() {
  sessions.clear();
}
