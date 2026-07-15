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
  it("renders conversation controls and delegates mode and composer changes", () => {
    const onModeChange = vi.fn();
    const onDraftChange = vi.fn();
    const onNewChat = vi.fn();
    render(
      <MemoryRouter>
        <ChatWorkspace
          conversations={[conversation]}
          activeConversationId={conversation.id}
          messages={conversation.messages}
          draft="draft text"
          mode="chat"
          isBusy={false}
          isSending={false}
          isGenerating={false}
          error={null}
          approvalResult={null}
          onNewChat={onNewChat}
          onSelectConversation={vi.fn()}
          onDeleteConversation={vi.fn()}
          onModeChange={onModeChange}
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
    fireEvent.click(screen.getByRole("button", { name: "Project" }));
    fireEvent.change(screen.getByLabelText("Chat message"), { target: { value: "changed" } });
    fireEvent.click(screen.getByRole("button", { name: "New Chat" }));
    expect(onModeChange).toHaveBeenCalledWith("project");
    expect(onDraftChange).toHaveBeenCalledWith("changed");
    expect(onNewChat).toHaveBeenCalledOnce();
  });
});
