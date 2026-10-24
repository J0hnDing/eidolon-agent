// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ChatWorkspace } from "./ChatWorkspace";
import { ComponentProps } from "react";

const conversation = {
  id: "project-1",
  title: "First project",
  mode: "project" as const,
  draft: "",
  messages: [{ id: 1, role: "assistant" as const, content: "Hello" }],
  createdAt: "2026-07-14T12:00:00Z",
  updatedAt: "2026-07-14T12:00:00Z",
};

afterEach(cleanup);

function workspaceProps(overrides: Partial<ComponentProps<typeof ChatWorkspace>> = {}): ComponentProps<typeof ChatWorkspace> {
  return {
    conversations: [conversation], activeConversationId: conversation.id, messages: conversation.messages,
    draft: "", mode: "project", isBusy: false, isSending: false, isGenerating: false, error: null,
    activeActTurnId: null, onNewConversation: vi.fn(), onSelectConversation: vi.fn(),
    onDeleteConversation: vi.fn(), onCancelAct: vi.fn(), onDraftChange: vi.fn(), onSubmit: vi.fn(),
    onApproveBuild: vi.fn(), onDenyBuild: vi.fn(), onApproveRuntime: vi.fn(), onDenyRuntime: vi.fn(),
    ...overrides,
  };
}

describe("ChatWorkspace", () => {
  it("renders quiet timestamps between conversation groups instead of on every turn", () => {
    const { container } = render(<MemoryRouter><ChatWorkspace {...workspaceProps({ messages: [
      { id: 1, role: "user", content: "First", createdAt: "2026-10-22T12:00:00" },
      { id: 2, role: "assistant", content: "Reply", createdAt: "2026-10-22T12:01:00Z" },
      { id: 3, role: "user", content: "Later", createdAt: "2026-10-22T15:01:00Z" },
    ] })} /></MemoryRouter>);
    const log = screen.getByRole("log", { name: "Conversation messages" });
    const timestamps = log.querySelectorAll(".chat-timestamp-separator time");
    expect(timestamps).toHaveLength(2);
    expect(timestamps[0].getAttribute("datetime")).toBe("2026-10-22T12:00:00.000Z");
    expect(timestamps[1].getAttribute("datetime")).toBe("2026-10-22T15:01:00.000Z");
    expect(container.querySelector("article time")).toBeNull();
  });

  it("uses a welcome suggestion to fill a draft without starting work", () => {
    const props = workspaceProps({ mode: "assistant", hideSidebar: true, messages: [] });
    render(<MemoryRouter><ChatWorkspace {...props} /></MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: "Plan my day" }));
    expect(props.onDraftChange).toHaveBeenCalledWith("Help me plan my day based on what needs my attention.");
    expect(props.onSubmit).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(screen.getByRole("textbox", { name: "Conversation message" }));
  });

  it("filters conversation history and selects the matching conversation", () => {
    const props = workspaceProps({ conversations: [{ ...conversation, title: "Plan a report" }, { ...conversation, id: "second", title: "Organize notes" }] });
    render(<MemoryRouter><ChatWorkspace {...props} /></MemoryRouter>);
    fireEvent.change(screen.getByRole("searchbox", { name: "Search conversations" }), { target: { value: "report" } });
    expect(screen.queryByRole("button", { name: /Organize notes/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Plan a report/ }));
    expect(props.onSelectConversation).toHaveBeenCalledWith(conversation.id);
  });
  it("shows an offset-free UTC session timestamp on the correct local day", () => {
    vi.stubEnv("TZ", "America/Toronto");
    try {
      render(
        <MemoryRouter>
          <ChatWorkspace
            conversations={[{ ...conversation, createdAt: "2026-09-27T02:17:00" }]}
            activeConversationId={conversation.id}
            messages={conversation.messages}
            draft=""
            mode="project"
            isBusy={false}
            isSending={false}
            isGenerating={false}
            error={null}
            onNewConversation={vi.fn()}
            onSelectConversation={vi.fn()}
            onDeleteConversation={vi.fn()}
            activeActTurnId={null}
            onCancelAct={vi.fn()}
            onDraftChange={vi.fn()}
            onSubmit={vi.fn()}
            onApproveBuild={vi.fn()}
            onDenyBuild={vi.fn()}
            onApproveRuntime={vi.fn()}
            onDenyRuntime={vi.fn()}
          />
        </MemoryRouter>,
      );
      const expected = new Date("2026-09-27T02:17:00Z").toLocaleString(undefined, {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
        timeZone: "America/Toronto",
      });
      expect(screen.getByText(`Created ${expected}`)).toBeTruthy();
    } finally {
      vi.unstubAllEnvs();
    }
  });

  it("shows session creation and WeCom ownership metadata instead of the mode headline", () => {
    render(
      <MemoryRouter>
        <ChatWorkspace
          conversations={[{
            ...conversation,
            id: "observer-7",
            mode: "observer",
            origin: "wecom",
            wecomUserId: "user-7",
            actSessionId: 7,
          }]}
          activeConversationId="observer-7"
          messages={conversation.messages}
          draft=""
          mode="observer"
          isBusy={false}
          isSending={false}
          isGenerating={false}
          error={null}
          onNewConversation={vi.fn()}
          onSelectConversation={vi.fn()}
          onDeleteConversation={vi.fn()}
          activeActTurnId={null}
          onCancelAct={vi.fn()}
          onDraftChange={vi.fn()}
          onSubmit={vi.fn()}
          onApproveBuild={vi.fn()}
          onDenyBuild={vi.fn()}
          onApproveRuntime={vi.fn()}
          onDenyRuntime={vi.fn()}
        />
      </MemoryRouter>,
    );

    expect(screen.getByText(/^Created /)).toBeTruthy();
    expect(screen.getByText("WeCom user user-7")).toBeTruthy();
    expect(screen.getByText("WeCom user user-7").closest("details")).toBeNull();
    expect(screen.queryByText("Details")).toBeNull();
    expect(screen.queryByText("Reads local context without making changes.")).toBeNull();
  });

  it("hides delete for the permanent Assistant chat while retaining it for other modes", () => {
    const onDeleteConversation = vi.fn();
    const renderWorkspace = (mode: "assistant" | "project") => (
      <MemoryRouter>
        <ChatWorkspace
          conversations={[{ ...conversation, mode }]}
          activeConversationId={conversation.id}
          messages={conversation.messages}
          draft=""
          mode={mode}
          isBusy={false}
          isSending={false}
          isGenerating={false}
          error={null}
          onNewConversation={vi.fn()}
          onSelectConversation={vi.fn()}
          onDeleteConversation={onDeleteConversation}
          activeActTurnId={null}
          onCancelAct={vi.fn()}
          onDraftChange={vi.fn()}
          onSubmit={vi.fn()}
          onApproveBuild={vi.fn()}
          onDenyBuild={vi.fn()}
          onApproveRuntime={vi.fn()}
          onDenyRuntime={vi.fn()}
        />
      </MemoryRouter>
    );

    const { rerender } = render(renderWorkspace("assistant"));
    expect(screen.queryByRole("button", { name: "Delete conversation" })).toBeNull();
    rerender(renderWorkspace("project"));
    fireEvent.click(screen.getByRole("button", { name: "Delete conversation" }));
    expect(onDeleteConversation).toHaveBeenCalledWith(conversation.id);
  });

  it("opens the new-chat popover at the bottom of the sidebar and creates each conversation mode", () => {
    const onDraftChange = vi.fn();
    const onNewConversation = vi.fn();
    const onDeleteConversation = vi.fn();
    const conversations = [
      conversation,
      { ...conversation, id: "act-1", title: "Do it", mode: "act" as const, actSessionId: 4 },
    ];
    render(
      <MemoryRouter>
        <ChatWorkspace
          conversations={conversations}
          activeConversationId={conversation.id}
          messages={conversation.messages}
          draft="draft text"
          mode="project"
          isBusy={false}
          isSending={false}
          isGenerating={false}
          error={null}
          onNewConversation={onNewConversation}
          onSelectConversation={vi.fn()}
          onDeleteConversation={onDeleteConversation}
          activeActTurnId={null}
          onCancelAct={vi.fn()}
          onDraftChange={onDraftChange}
          onSubmit={vi.fn()}
          onApproveBuild={vi.fn()}
          onDenyBuild={vi.fn()}
          onApproveRuntime={vi.fn()}
          onDenyRuntime={vi.fn()}
        />
      </MemoryRouter>,
    );

    expect(screen.getByText("Hello")).toBeTruthy();
    expect(screen.getByText("Eidolon")).toBeTruthy();
    expect(screen.queryByText("assistant")).toBeNull();
    expect(screen.getAllByText("Project").length).toBeGreaterThan(0);
    expect(screen.getByText("Act")).toBeTruthy();
    const sidebar = screen.getByRole("complementary", { name: "Chats" });
    expect(sidebar.querySelector(".chat-thread-list")?.nextElementSibling?.classList.contains("chat-create-actions")).toBe(true);
    expect(screen.getByRole("button", { name: "New Chat" })).toBeTruthy();
    expect(screen.queryByRole("dialog", { name: "Create conversation" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "New Chat" }));
    expect(screen.getByRole("dialog", { name: "Create conversation" })).toBeTruthy();
    expect(screen.queryByText("Choose a conversation mode.")).toBeNull();
    expect(screen.queryByRole("button", { name: "Close new chat menu" })).toBeNull();
    expect(screen.getByRole("button", { name: "Create Project" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Create Act" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Create Project" }).classList.contains("project")).toBe(true);
    expect(screen.getByRole("button", { name: "Create Act" }).classList.contains("act")).toBe(true);
    expect(screen.getByRole("button", { name: "Create Observer" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Open Assistant" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Create Project" }));
    fireEvent.click(screen.getByRole("button", { name: "New Chat" }));
    fireEvent.click(screen.getByRole("button", { name: "Create Act" }));
    fireEvent.click(screen.getByRole("button", { name: "New Chat" }));
    fireEvent.click(screen.getByRole("button", { name: "Create Observer" }));
    fireEvent.click(screen.getByRole("button", { name: "New Chat" }));
    fireEvent.click(screen.getByRole("button", { name: "Open Assistant" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete conversation" }));
    fireEvent.change(screen.getByLabelText("Conversation message"), { target: { value: "changed" } });
    expect(onNewConversation).toHaveBeenNthCalledWith(1, "project");
    expect(onNewConversation).toHaveBeenNthCalledWith(2, "act");
    expect(onNewConversation).toHaveBeenNthCalledWith(3, "observer");
    expect(onNewConversation).toHaveBeenNthCalledWith(4, "assistant");
    expect(onDraftChange).toHaveBeenCalledWith("changed");
    expect(onDeleteConversation).toHaveBeenCalledWith(conversation.id);
    expect(onNewConversation).toHaveBeenCalledTimes(4);
    expect(screen.getByRole("button", { name: "Send message" })).toBeTruthy();
  });

  it("shows one Act thinking state and keeps work metadata collapsed", () => {
    const messages = [
      { id: 2, role: "user" as const, content: "Check my goals", actTurnId: 7 },
      {
        id: 3,
        role: "assistant" as const,
        content: "Eidolon is thinking...",
        kind: "thinking" as const,
        actTurnId: 7,
        actWork: {
          status: "running",
          activities: [{ kind: "started", label: "Act is working" }],
          startedAt: new Date().toISOString(),
          completedAt: null,
        },
      },
    ];
    const { rerender } = render(
      <MemoryRouter>
        <ChatWorkspace
          conversations={[{ ...conversation, id: "act-1", mode: "act", messages, actSessionId: 4 }]}
          activeConversationId="act-1"
          messages={messages}
          draft=""
          mode="act"
          isBusy
          isSending
          isGenerating={false}
          error={null}
          onNewConversation={vi.fn()}
          onSelectConversation={vi.fn()}
          onDeleteConversation={vi.fn()}
          activeActTurnId={7}
          onCancelAct={vi.fn()}
          onDraftChange={vi.fn()}
          onSubmit={vi.fn()}
          onApproveBuild={vi.fn()}
          onDenyBuild={vi.fn()}
          onApproveRuntime={vi.fn()}
          onDenyRuntime={vi.fn()}
        />
      </MemoryRouter>,
    );

    expect(screen.getAllByText("Eidolon is thinking...")).toHaveLength(1);

    const completedMessages = [{
      ...messages[1],
      content: "Done",
      kind: "text" as const,
      actWork: {
        status: "succeeded",
        activities: [{ kind: "mcpToolCall", label: "Used atlas.goals.list" }],
        startedAt: "2026-08-31T12:00:00Z",
        completedAt: "2026-08-31T12:00:05Z",
      },
    }];
    rerender(
      <MemoryRouter>
        <ChatWorkspace
          conversations={[{ ...conversation, id: "act-1", mode: "act", messages: completedMessages, actSessionId: 4 }]}
          activeConversationId="act-1"
          messages={completedMessages}
          draft=""
          mode="act"
          isBusy={false}
          isSending={false}
          isGenerating={false}
          error={null}
          onNewConversation={vi.fn()}
          onSelectConversation={vi.fn()}
          onDeleteConversation={vi.fn()}
          activeActTurnId={null}
          onCancelAct={vi.fn()}
          onDraftChange={vi.fn()}
          onSubmit={vi.fn()}
          onApproveBuild={vi.fn()}
          onDenyBuild={vi.fn()}
          onApproveRuntime={vi.fn()}
          onDenyRuntime={vi.fn()}
        />
      </MemoryRouter>,
    );

    const details = screen.getByText("Worked for 5s · 1 tool used").closest("details");
    expect(details?.open).toBe(false);
    fireEvent.click(screen.getByText("Worked for 5s · 1 tool used"));
    expect(details?.open).toBe(true);
    expect(screen.getByText("Used atlas.goals.list")).toBeTruthy();
  });
});
