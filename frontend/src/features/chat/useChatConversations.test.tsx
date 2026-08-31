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
    expect(result.current.mode).toBe("project");
    expect(result.current.messages[result.current.messages.length - 1]?.content).toBe("Build a tool");

    act(() => result.current.createNewConversation("project"));
    const newProjectId = result.current.activeConversationId;
    expect(result.current.activeConversationId).not.toBe(firstId);
    expect(result.current.mode).toBe("project");
    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0]?.content).toBe("Hi, what can I build for you today?");
    const firstConversationMessages = result.current.conversations.find((item) => item.id === firstId)?.messages ?? [];
    expect(firstConversationMessages[firstConversationMessages.length - 1]?.content).toBe("Build a tool");

    act(() => result.current.selectConversation(firstId));
    expect(result.current.messages[result.current.messages.length - 1]?.content).toBe("Build a tool");
    act(() => result.current.selectConversation(newProjectId));
    expect(result.current.messages).toHaveLength(1);
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

  it("never renders another conversation while a selected id is not present", () => {
    const { result } = renderHook(() => useChatConversations());

    act(() => result.current.appendMessages([{ id: 99, role: "user", content: "Private first transcript" }]));
    act(() => result.current.selectConversation("conversation-not-loaded-yet"));

    expect(result.current.activeConversation).toBeUndefined();
    expect(result.current.messages).toEqual([
      { id: 1, role: "assistant", content: "Hi, what can I build for you today?" },
    ]);
    expect(result.current.messages.some((message) => message.content === "Private first transcript")).toBe(false);
  });

  it("drops stored direct-chat conversations instead of converting them into projects", () => {
    localStorage.setItem("personal-agent.chat-conversations.v1", JSON.stringify([{
      id: "legacy-chat",
      title: "Legacy chat",
      mode: "chat",
      draft: "Do not reinterpret this",
      messages: [{ id: 1, role: "user", content: "Ordinary question" }],
      createdAt: "2026-08-30T12:00:00Z",
      updatedAt: "2026-08-30T12:00:00Z",
    }]));

    const { result } = renderHook(() => useChatConversations());

    expect(result.current.mode).toBe("project");
    expect(result.current.conversations).toHaveLength(1);
    expect(result.current.conversations[0].id).not.toBe("legacy-chat");
    expect(result.current.messages[0]?.content).toBe("Hi, what can I build for you today?");
  });

  it("updates only the exact retired starter lines in stored Project and Act conversations", () => {
    localStorage.setItem("personal-agent.chat-conversations.v1", JSON.stringify([
      {
        id: "project-1",
        title: "New project",
        mode: "project",
        draft: "",
        messages: [{ id: 1, role: "assistant", content: "Project is ready to plan a reusable skill proposal." }],
        createdAt: "2026-08-30T12:00:00Z",
        updatedAt: "2026-08-30T12:00:00Z",
      },
      {
        id: "act-1",
        title: "New act",
        mode: "act",
        draft: "",
        messages: [
          { id: 1, role: "assistant", content: "Act is ready to work persistently in its shared workspace using Eidolon tools." },
          { id: 2, role: "assistant", content: "Keep this real response." },
        ],
        createdAt: "2026-08-30T12:00:00Z",
        updatedAt: "2026-08-30T12:00:00Z",
      },
    ]));

    const { result } = renderHook(() => useChatConversations());

    expect(result.current.conversations[0].messages[0].content).toBe("Hi, what can I build for you today?");
    expect(result.current.conversations[1].messages.map((message) => message.content)).toEqual([
      "Hi, what can I do for you?",
      "Keep this real response.",
    ]);
  });
});
