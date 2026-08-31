// @vitest-environment jsdom

import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ChatWorkspace } from "./ChatWorkspace";

const conversation = {
  id: "chat-1",
  title: "First chat",
  mode: "chat" as const,
  draft: "",
  messages: [{ id: 1, role: "assistant" as const, content: "Hello" }],
  createdAt: "2026-07-14T12:00:00Z",
  updatedAt: "2026-07-14T12:00:00Z",
};

describe("ChatWorkspace", () => {
  it("marks immutable conversation modes and creates a selected mode", () => {
    const onDraftChange = vi.fn();
    const onNewConversation = vi.fn();
    const conversations = [
      conversation,
      { ...conversation, id: "project-1", title: "Build it", mode: "project" as const },
      { ...conversation, id: "act-1", title: "Do it", mode: "act" as const, actSessionId: 4 },
    ];
    render(
      <MemoryRouter>
        <ChatWorkspace
          conversations={conversations}
          activeConversationId={conversation.id}
          messages={conversation.messages}
          draft="draft text"
          mode="chat"
          isBusy={false}
          isSending={false}
          isGenerating={false}
          error={null}
          onNewConversation={onNewConversation}
          onSelectConversation={vi.fn()}
          onDeleteConversation={vi.fn()}
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
    expect(screen.getAllByText("Chat").length).toBeGreaterThan(0);
    expect(screen.getByText("Project")).toBeTruthy();
    expect(screen.getByText("Act")).toBeTruthy();
    expect(screen.queryByLabelText("Assistant mode")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "New Project" }));
    fireEvent.click(screen.getByRole("button", { name: "New Act" }));
    fireEvent.change(screen.getByLabelText("Chat message"), { target: { value: "changed" } });
    fireEvent.click(screen.getByRole("button", { name: "New Chat" }));
    expect(onNewConversation).toHaveBeenNthCalledWith(1, "project");
    expect(onNewConversation).toHaveBeenNthCalledWith(2, "act");
    expect(onDraftChange).toHaveBeenCalledWith("changed");
    expect(onNewConversation).toHaveBeenLastCalledWith("chat");
  });
});
