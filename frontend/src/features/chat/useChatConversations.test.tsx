// @vitest-environment jsdom

import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { clearManagedSessionCache } from "./managedSessionCache";
import { useChatConversations } from "./useChatConversations";
import { CHAT_STORAGE_KEY, ACTIVE_CONVERSATION_KEY, createConversation } from "../../lib/chatStore";

describe("useChatConversations", () => {
  afterEach(cleanup);
  beforeEach(() => { localStorage.clear(); clearManagedSessionCache(); });

  it("keeps each conversation home scoped while preserving builds and agent metadata", () => {
    const build = { ...createConversation("project", { id: "existing-build" }), draft: "Keep this build draft" };
    const observer = createConversation("observer", { id: "existing-observer", actSessionId: 71 });
    localStorage.setItem(CHAT_STORAGE_KEY, JSON.stringify([build, observer]));
    localStorage.setItem(ACTIVE_CONVERSATION_KEY, observer.id);
    const { result, unmount } = renderHook(() => useChatConversations("project"));
    expect(result.current.mode).toBe("project");
    expect(result.current.activeConversationId).toBe(build.id);
    expect(result.current.draft).toBe("Keep this build draft");
    expect(result.current.conversations).toHaveLength(1);
    act(() => result.current.createNewConversation("project"));
    const selectedBuildId = result.current.activeConversationId;
    unmount();
    const agentView = renderHook(() => useChatConversations("observer"));
    expect(agentView.result.current.mode).toBe("observer");
    act(() => agentView.result.current.updateDraft("Private observer draft"));
    expect(localStorage.getItem(CHAT_STORAGE_KEY)).not.toContain("Private observer draft");
    agentView.unmount();
    const reopenedBuilds = renderHook(() => useChatConversations("project"));
    expect(reopenedBuilds.result.current.activeConversationId).toBe(selectedBuildId);
    reopenedBuilds.unmount();
    const reopenedObserver = renderHook(() => useChatConversations("observer"));
    expect(reopenedObserver.result.current.draft).toBe("Private observer draft");
    expect(reopenedObserver.result.current.conversations[0].actSessionId).toBe(71);
    reopenedObserver.unmount();
  });

  it("keeps a deleted home's replacement in the same mode", () => {
    const { result } = renderHook(() => useChatConversations("project"));
    const first = result.current.activeConversationId;
    act(() => result.current.deleteConversation(first));
    expect(result.current.mode).toBe("project");
    expect(result.current.activeConversationId).not.toBe(first);
    expect(result.current.conversations).toHaveLength(1);
  });

  it("keeps agent bindings distinct and removes pruned sessions without changing a valid selection", () => {
    const { result } = renderHook(() => useChatConversations());
    const session = { id: 7, title: "Session", origin: "web", status: "active",
      created_at: "2026-09-05T12:00:00Z", updated_at: "2026-09-05T12:00:00Z", wecom_user_id: null };
    act(() => result.current.importAgentSessions("observer", [session]));
    act(() => result.current.importAgentSessions("assistant", [session]));
    act(() => result.current.selectConversation("assistant-7"));
    act(() => result.current.importAgentSessions("observer", []));
    expect(result.current.activeConversationId).toBe("assistant-7");
    expect(result.current.mode).toBe("assistant");
    expect(result.current.conversations.some((item) => item.id === "observer-7")).toBe(false);
    act(() => result.current.importAgentSessions("assistant", []));
    expect(result.current.activeConversation).toBeDefined();
    expect(result.current.mode).toBe("assistant");
  });

  it("owns drafts and creates conversations with immutable modes", () => {
    const { result } = renderHook(() => useChatConversations());
    const firstId = result.current.activeConversationId;

    act(() => result.current.updateDraft("Build a tool"));
    act(() => result.current.appendMessages([{ id: 99, role: "user", content: "Build a tool" }]));
    expect(result.current.draft).toBe("Build a tool");
    expect(result.current.mode).toBe("assistant");
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
      wecom_user_id: null,
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
      { id: 1, role: "assistant", content: "Hi, what can I do for you?" },
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

    expect(result.current.mode).toBe("assistant");
    expect(result.current.conversations).toHaveLength(1);
    expect(result.current.conversations[0].id).not.toBe("legacy-chat");
    expect(result.current.messages[0]?.content).toBe("Hi, what can I do for you?");
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
    ]);
    expect(localStorage.getItem(CHAT_STORAGE_KEY)).not.toContain("Keep this real response.");
  });

  it("purges legacy managed transcripts from storage while preserving Project history", () => {
    const legacy = [
      {
        id: "assistant-8", title: "Assistant", mode: "assistant", actSessionId: 8,
        draft: "unsent managed draft", messages: [{ id: 2, role: "user", content: "assistant secret" }],
        createdAt: "2026-09-01T00:00:00Z", updatedAt: "2026-09-01T00:00:00Z",
      },
      {
        id: "observer-9", title: "Observer", mode: "observer", actSessionId: 9,
        messages: [{ id: 3, role: "assistant", content: "observer secret", actWork: { activities: [{ label: "private activity" }] } }],
        createdAt: "2026-09-01T00:00:00Z", updatedAt: "2026-09-01T00:00:00Z",
      },
      {
        id: "project-2", title: "Project", mode: "project", draft: "Project draft",
        messages: [{ id: 4, role: "user", content: "Project transcript stays" }],
        createdAt: "2026-09-01T00:00:00Z", updatedAt: "2026-09-01T00:00:00Z",
      },
    ];
    localStorage.setItem(CHAT_STORAGE_KEY, JSON.stringify(legacy));

    const { result } = renderHook(() => useChatConversations());
    const stored = localStorage.getItem(CHAT_STORAGE_KEY) ?? "";

    expect(stored).not.toContain("assistant secret");
    expect(stored).not.toContain("observer secret");
    expect(stored).not.toContain("private activity");
    expect(JSON.parse(stored)).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: "assistant-8", actSessionId: 8 }),
      expect.objectContaining({ id: "observer-9", actSessionId: 9 }),
      expect.objectContaining({ id: "project-2", messages: legacy[2].messages, draft: "Project draft" }),
    ]));
    expect(JSON.parse(stored).find((item: { id: string }) => item.id === "assistant-8")).not.toHaveProperty("messages");
    expect(result.current.conversations.find((item) => item.id === "assistant-8")?.messages[0]?.content)
      .toBe("Hi, what can I do for you?");
    expect(result.current.conversations.find((item) => item.id === "project-2")?.messages[0]?.content)
      .toBe("Project transcript stays");
  });

  it("keeps hydrated managed history in memory without persisting replies or drafts", () => {
    const { result } = renderHook(() => useChatConversations());
    const assistant = { id: 11, title: "Assistant", origin: "web", status: "active",
      created_at: "2026-09-05T12:00:00Z", updated_at: "2026-09-05T12:00:00Z", wecom_user_id: null };
    act(() => result.current.importAgentSessions("assistant", [assistant]));
    act(() => result.current.selectConversation("assistant-11"));
    act(() => result.current.updateConversation("assistant-11", (conversation) => ({
      ...conversation,
      messages: [
        { id: 1, role: "assistant", content: "Hi, what can I do for you?" },
        { id: 2, role: "user", content: "Server loaded question" },
        { id: 3, role: "assistant", content: "Server loaded answer" },
      ],
    })));
    act(() => result.current.updateDraft("private unsent input"));

    expect(result.current.messages.map((message) => message.content)).toEqual([
      "Hi, what can I do for you?", "Server loaded question", "Server loaded answer",
    ]);
    const stored = localStorage.getItem(CHAT_STORAGE_KEY) ?? "";
    expect(stored).not.toContain("Server loaded question");
    expect(stored).not.toContain("Server loaded answer");
    expect(stored).not.toContain("private unsent input");
    expect(JSON.parse(stored).find((item: { id: string }) => item.id === "assistant-11")).toMatchObject({
      id: "assistant-11", actSessionId: 11,
    });

    act(() => result.current.createNewConversation("project"));
    act(() => result.current.appendMessages([{ id: 21, role: "user", content: "Project history stays saved" }]));
    expect(localStorage.getItem(CHAT_STORAGE_KEY)).toContain("Project history stays saved");
  });
});
