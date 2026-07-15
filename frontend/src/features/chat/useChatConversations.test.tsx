// @vitest-environment jsdom

import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { useChatConversations } from "./useChatConversations";

describe("useChatConversations", () => {
  beforeEach(() => localStorage.clear());

  it("owns draft, mode, message, and conversation transitions", () => {
    const { result } = renderHook(() => useChatConversations());
    const firstId = result.current.activeConversationId;

    act(() => result.current.updateDraft("Build a tool"));
    act(() => result.current.updateMode("project"));
    act(() => result.current.appendMessages([{ id: 99, role: "user", content: "Build a tool" }]));
    expect(result.current.draft).toBe("Build a tool");
    expect(result.current.mode).toBe("project");
    expect(result.current.messages[result.current.messages.length - 1]?.content).toBe("Build a tool");

    act(() => result.current.createNewConversation());
    expect(result.current.activeConversationId).not.toBe(firstId);
    act(() => result.current.deleteConversation(result.current.activeConversationId));
    expect(result.current.conversations.length).toBeGreaterThan(0);
  });
});
