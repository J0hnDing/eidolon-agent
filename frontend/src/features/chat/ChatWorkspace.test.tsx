// @vitest-environment jsdom

import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ChatWorkspace } from "./ChatWorkspace";

const conversation = {
  id: "project-1",
  title: "First project",
  mode: "project" as const,
  draft: "",
  messages: [{ id: 1, role: "assistant" as const, content: "Hello" }],
  createdAt: "2026-07-14T12:00:00Z",
  updatedAt: "2026-07-14T12:00:00Z",
};

describe("ChatWorkspace", () => {
  it("marks immutable conversation modes and creates a selected mode", () => {
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
    expect(screen.queryByRole("button", { name: "New Chat" })).toBeNull();
    expect(screen.queryByLabelText("Assistant mode")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "New Project" }));
    fireEvent.click(screen.getByRole("button", { name: "New Act" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete conversation" }));
    fireEvent.change(screen.getByLabelText("Conversation message"), { target: { value: "changed" } });
    expect(onNewConversation).toHaveBeenNthCalledWith(1, "project");
    expect(onNewConversation).toHaveBeenNthCalledWith(2, "act");
    expect(onDraftChange).toHaveBeenCalledWith("changed");
    expect(onDeleteConversation).toHaveBeenCalledWith(conversation.id);
    expect(onNewConversation).toHaveBeenCalledTimes(2);
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
