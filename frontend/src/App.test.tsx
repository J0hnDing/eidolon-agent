// @vitest-environment jsdom

import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import App from "./App";

vi.mock("./pages/ChatPage", () => ({ default: () => <div>Chat content</div> }));

describe("App navigation", () => {
  it("groups every primary destination into a clear application shell", () => {
    render(
      <MemoryRouter initialEntries={["/chat"]}>
        <App />
      </MemoryRouter>,
    );

    const navigation = screen.getByRole("navigation", { name: "Primary navigation" });
    expect(within(navigation).getByText("Workspace")).toBeTruthy();
    expect(within(navigation).getByText("Capabilities")).toBeTruthy();
    expect(within(navigation).getByText("Control")).toBeTruthy();

    expect(within(navigation).getByRole("link", { name: "Chat" }).getAttribute("href")).toBe("/chat");
    expect(within(navigation).queryByRole("link", { name: "Act" })).toBeNull();
    expect(within(navigation).getByRole("link", { name: "Applications" }).getAttribute("href")).toBe("/apps");
    expect(within(navigation).getByRole("link", { name: "Approvals" }).getAttribute("href")).toBe("/approval-requests");
    expect(within(navigation).getByRole("link", { name: "Settings" }).getAttribute("href")).toBe("/settings");
    expect(navigation.querySelectorAll(".nav-active-indicator")).toHaveLength(1);
    expect(screen.getByRole("link", { name: "Skip to content" }).getAttribute("href")).toBe("#main-content");
  });
});
