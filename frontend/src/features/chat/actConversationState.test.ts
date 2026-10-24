import { describe, expect, it } from "vitest";
import { ActLiveMessage, ActSession, ActTurn } from "../../api/client";
import { actSessionMessages, awaitingActReply } from "./actConversationState";

const notice: ActLiveMessage = {
  id: 12, role: "assistant", text: "Enter 123 in Duo Mobile.", reply_to_message_id: null,
  reply_status: "awaiting", reply_expires_at: "2026-10-09T13:00:00", created_at: "2026-10-09T12:00:00Z",
};
const turn: ActTurn = {
  id: 5, session_id: 1, codex_turn_id: null, user_message: "Sign in", assistant_message: null,
  activity_json: [], status: "running", error_message: null, cancel_requested_at: null,
  delivery_message_thread_id: null, delivery_status: null, created_at: "2026-10-09T12:00:00Z",
  started_at: "2026-10-09T12:00:00Z", completed_at: null, live_messages: [notice],
};

describe("Act live messages", () => {
  it("preserves backend times for requests, live messages, and final replies", () => {
    const completedAt = "2026-10-09T15:00:00Z";
    const session: ActSession = {
      id: 1, title: "History", origin: "web", status: "active", wecom_user_id: null,
      created_at: turn.created_at, updated_at: completedAt,
      turns: [{ ...turn, status: "completed", completed_at: completedAt, assistant_message: "Done" }],
    };
    expect(actSessionMessages(session, "act").slice(1).map((message) => message.createdAt))
      .toEqual([turn.created_at, notice.created_at, completedAt]);
  });

  it("renders a retained reply without an empty user bubble after its older request expires", () => {
    const session: ActSession = {
      id: 1, title: "Retained history", origin: "web", status: "active", wecom_user_id: null,
      created_at: turn.created_at, updated_at: turn.created_at,
      turns: [{ ...turn, status: "succeeded", user_message: "", assistant_message: "Retained full reply", live_messages: [] }],
    };
    expect(actSessionMessages(session, "act").slice(1).map((message) => [message.role, message.content]))
      .toEqual([["assistant", "Retained full reply"]]);
  });

  it("keeps background Assistant prompts and quiet activations out of the conversation", () => {
    const session: ActSession = {
      id: 1, title: "Assistant", origin: "web", status: "active", wecom_user_id: null,
      created_at: turn.created_at, updated_at: turn.created_at, turns: [
        { ...turn, id: 5, user_message: "Internal review context", backend_message_kind: "assistant_daily", status: "completed", assistant_message: "", live_messages: [], activity_json: [{ kind: "tool", label: "Read calendar" }] },
        { ...turn, id: 6, user_message: "Internal agenda context", assistant_context_json: { kind: "agenda" }, status: "completed", assistant_message: "How did the interview go?", live_messages: [] },
        { ...turn, id: 7, user_message: "Internal reflection context", backend_message_kind: "assistant_reflection", status: "failed", assistant_message: null, error_message: "Review could not finish", live_messages: [] },
      ],
    };
    expect(actSessionMessages(session, "assistant").slice(1).map((message) => message.content)).toEqual([
      "How did the interview go?", "Review could not finish",
    ]);
    session.turns[0].status = "running";
    session.turns[0].live_messages = [notice];
    expect(actSessionMessages(session, "assistant").slice(1).map((message) => message.content)).toEqual([
      notice.text, "How did the interview go?", "Review could not finish",
    ]);
  });

  it("orders the original request, notices and replies before thinking or the final answer", () => {
    const session = {
      id: 1, title: "Sign in", origin: "web", status: "active", wecom_user_id: null,
      created_at: turn.created_at, updated_at: turn.created_at, turns: [{
        ...turn, live_messages: [notice, {
          ...notice, id: 13, role: "user" as const, text: "Done", reply_to_message_id: 12, reply_status: "none" as const,
        }],
      }],
    };
    expect(actSessionMessages(session, "act").slice(1).map((message) => message.content))
      .toEqual(["Sign in", notice.text, "Done", "Eidolon is thinking..."]);
    session.turns[0].status = "completed";
    session.turns[0].assistant_message = "Signed in";
    expect(actSessionMessages(session, "act").slice(-1)[0]?.content).toBe("Signed in");
    expect(new Set(actSessionMessages(session, "act").map((message) => message.id)).size).toBe(5);
  });

  it("accepts a reply only while an assistant request is active and before its UTC deadline", () => {
    const beforeDeadline = Date.parse("2026-10-09T12:59:59Z");
    expect(awaitingActReply(turn, beforeDeadline)?.id).toBe(12);
    expect(awaitingActReply(turn, Date.parse("2026-10-09T13:00:00Z"))).toBeNull();
    expect(awaitingActReply({ ...turn, status: "completed" }, beforeDeadline)).toBeNull();
    expect(awaitingActReply({ ...turn, live_messages: undefined }, beforeDeadline)).toBeNull();
    for (const status of ["answered", "expired", "cancelled", "failed", "none"] as const) {
      expect(awaitingActReply({ ...turn, live_messages: [{ ...notice, reply_status: status }] }, beforeDeadline)).toBeNull();
    }
    expect(awaitingActReply({ ...turn, live_messages: [{ ...notice, reply_expires_at: null }] }, beforeDeadline)).toBeNull();
    expect(awaitingActReply({ ...turn, live_messages: [{ ...notice, role: "user" }] }, beforeDeadline)).toBeNull();
  });
});
