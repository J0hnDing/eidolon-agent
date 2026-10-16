import { ActLiveMessage, ActSession, ActTurn, AgentId } from "../../api/client";
import { ChatMessage, initialMessagesForMode } from "../../lib/chatStore";
import { parseBackendDateTime } from "../../lib/dateTime";

export function isActiveActTurn(turn: ActTurn): boolean {
  return turn.status === "queued" || turn.status === "running";
}

export function awaitingActReply(turn: ActTurn | null, now = Date.now()): ActLiveMessage | null {
  if (!turn || !isActiveActTurn(turn)) return null;
  return [...(turn.live_messages ?? [])].reverse().find((message) =>
    message.role === "assistant"
    && message.reply_status === "awaiting"
    && message.reply_expires_at !== null
    && parseBackendDateTime(message.reply_expires_at).getTime() > now,
  ) ?? null;
}

export function actSessionMessages(session: ActSession, agentId: AgentId): ChatMessage[] {
  return [
    ...initialMessagesForMode(agentId),
    ...session.turns.flatMap((turn) => {
      const active = isActiveActTurn(turn);
      const result = active
        ? "Eidolon is thinking..."
        : turn.assistant_message ?? turn.error_message ?? `${agentId} did not return a response.`;
      return [
        { id: turn.id * 10 + 2, role: "user" as const, content: turn.user_message, actTurnId: turn.id },
        ...(turn.live_messages ?? []).map((message) => ({
          id: -message.id,
          role: message.role,
          content: message.text,
          actTurnId: turn.id,
        })),
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
