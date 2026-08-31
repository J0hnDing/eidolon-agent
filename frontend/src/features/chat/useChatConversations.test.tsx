// @vitest-environment jsdom

import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { useChatConversations } from "./useChatConversations";

describe("useChatConversations", () => {
  beforeEach(() => localStorage.clear());

  it("owns drafts and creates conversations with immutable modes", () => {
    const { result } = renderHook(() => useChatConversations());
    const firstId = result.current.activeConversationId;

    act(() => result.current.updateDraft("Build a tool"));
    act(() => result.current.appendMessages([{ id: 99, role: "user", content: "Build a tool" }]));
    expect(result.current.draft).toBe("Build a tool");
    expect(result.current.mode).toBe("chat");
    expect(result.current.messages[result.current.messages.length - 1]?.content).toBe("Build a tool");

    act(() => result.current.createNewConversation("project"));
    expect(result.current.activeConversationId).not.toBe(firstId);
    expect(result.current.mode).toBe("project");
    act(() => result.current.importActSessions([{
      id: 7,
      title: "Persistent task",
      origin: "web",
      status: "active",
      created_at: "2026-08-30T12:00:00Z",
      updated_at: "2026-08-30T12:01:00Z",
    }]));
    expect(result.current.conversations.find((item) => item.actSessionId === 7)?.mode).toBe("act");
    act(() => result.current.deleteConversation(result.current.activeConversationId));
    expect(result.current.conversations.length).toBeGreaterThan(0);
  });
});
