// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ConversationMode } from "./api/client";
import App from "./App";
import { createConversation, saveActiveConversationId, saveStoredConversations } from "./lib/chatStore";
import { usePendingApprovalCount } from "./lib/usePendingApprovalCount";

vi.mock("./lib/usePendingApprovalCount", () => ({
  usePendingApprovalCount: vi.fn(),
}));

vi.mock("./pages/ActivityPage", async () => {
  const { useLocation } = await import("react-router-dom");
  return {
    default: function ActivityRoute() {
      const location = useLocation();
      return <div data-testid="activity-route">{location.pathname}{location.search}</div>;
    },
  };
});

vi.mock("./pages/AgentsPage", async () => {
  const { useLocation } = await import("react-router-dom");
  return {
    default: function AgentsRoute() {
      const location = useLocation();
      return <div data-testid="agents-route">{location.pathname}{location.search}</div>;
    },
  };
});

vi.mock("./pages/AssistantPage", () => ({ default: () => <div data-testid="assistant-page">Assistant page</div>, AssistantDetailsPage: () => <div data-testid="assistant-details-page">Assistant details</div> }));
vi.mock("./pages/FunctionsSettingsPage", () => ({ default: () => <div data-testid="functions-settings-page">Functions settings</div> }));
vi.mock("./pages/IntegrationsPage", () => ({ default: () => <div>Integrations page</div> }));
vi.mock("./pages/MemoryPage", () => ({ default: () => <div>Memory page</div> }));
vi.mock("./pages/SkillBuildPage", () => ({ default: () => <div data-testid="skill-build-page">Skill build page</div> }));
vi.mock("./pages/SkillDetailPage", () => ({ default: () => <div>Skill detail page</div> }));
vi.mock("./pages/SkillsPage", () => ({ default: () => <div>Skills page</div> }));
vi.mock("./pages/SchedulesPage", () => ({ default: () => <div data-testid="automations-page">Automations page</div> }));
vi.mock("./pages/UsageSettingsPage", () => ({ default: () => <div data-testid="settings-page">Settings page</div> }));
vi.mock("./pages/WebAppPage", () => ({ default: () => <div>Application page</div> }));
vi.mock("./pages/WebAppsPage", () => ({ default: () => <div>Applications page</div> }));

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  localStorage.clear();
  vi.mocked(usePendingApprovalCount).mockReturnValue(3);
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("App navigation", () => {
  it("finds a settings destination with the keyboard page switcher", async () => {
    renderAt("/assistant");
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    const search = screen.getByRole("combobox", { name: "Find a page" });
    fireEvent.change(search, { target: { value: "models" } });
    expect(screen.getAllByRole("option")).toHaveLength(1);
    fireEvent.keyDown(search, { key: "Enter" });
    expect(await screen.findByTestId("settings-page")).toBeTruthy();
    expect(screen.queryByRole("dialog", { name: "Go to…" })).toBeNull();
  });

  it("closes the page switcher with Escape and restores its trigger", () => {
    renderAt("/assistant");
    const trigger = screen.getByRole("button", { name: /Go to…/ });
    trigger.focus();
    fireEvent.click(trigger);
    expect(screen.getByRole("dialog", { name: "Go to…" })).toBeTruthy();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Go to…" })).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });
  it("shows the new primary navigation, Activity badge, and bottom Settings link", () => {
    renderAt("/assistant");

    const navigation = screen.getByRole("navigation", { name: "Primary navigation" });
    expect(within(navigation).getByText("Workspace")).toBeTruthy();
    expect(within(navigation).queryByText("Personal")).toBeNull();
    expect(screen.queryByText(/Your workspace|Local workspace|Personal workspace|Local & private/i)).toBeNull();
    expect(within(navigation).queryByText("Capabilities")).toBeNull();
    expect(within(navigation).queryByText("Control")).toBeNull();
    expect(within(navigation).queryByRole("link", { name: "Chat" })).toBeNull();

    const destinations = [
      ["Assistant", "/assistant"],
      ["Agents", "/agents"],
      ["Applications", "/apps"],
      ["Skills", "/skills"],
      ["Automations", "/automations"],
      ["Memory", "/memory"],
      ["Activity", "/activity"],
    ];
    for (const [label, href] of destinations) {
      const linkName = label === "Activity" ? /^Activity/ : label;
      expect(within(navigation).getByRole("link", { name: linkName }).getAttribute("href")).toBe(href);
    }

    const activityLink = within(navigation).getByRole("link", { name: /^Activity/ });
    expect(within(activityLink).getByLabelText("3 pending approvals").textContent).toBe("3");

    const settingsNavigation = screen.getByRole("navigation", { name: "Settings navigation" });
    expect(within(settingsNavigation).getByRole("link", { name: "Settings" }).getAttribute("href")).toBe("/settings");
    expect(screen.getByRole("link", { name: "Skip to content" }).getAttribute("href")).toBe("#main-content");
  });

  it("opens and closes the mobile navigation with Escape", () => {
    renderAt("/assistant");

    const toggle = screen.getByRole("button", { name: "Open navigation" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");

    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(document.getElementById("app-navigation")?.className).toContain("is-open");

    fireEvent.keyDown(window, { key: "Escape" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
  });

  it("closes mobile navigation after a destination changes", async () => {
    renderAt("/assistant");
    const toggle = screen.getByRole("button", { name: "Open navigation" });
    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole("link", { name: "Skills" }));

    await screen.findByText("Skills page");
    await waitFor(() => expect(toggle.getAttribute("aria-expanded")).toBe("false"));
  });
});

describe("legacy routes", () => {
  it.each([
    ["project", "project-old", undefined, "Skill build page"],
    ["act", "act-old", 42, "/agents/act?session=42"],
    ["observer", "observer-old", 17, "/agents/observer?session=17"],
    ["assistant", "assistant-old", 9, "Assistant page"],
  ] as Array<[ConversationMode, string, number | undefined, string]>)
  ("redirects legacy /chat using the active %s conversation", async (mode, id, actSessionId, destination) => {
    const conversation = createConversation(mode, { id, actSessionId });
    saveStoredConversations([conversation]);
    saveActiveConversationId(id);

    renderAt("/chat");

    if (mode === "project") expect(await screen.findByTestId("skill-build-page")).toBeTruthy();
    else if (mode === "act" || mode === "observer") {
      expect((await screen.findByTestId("agents-route")).textContent).toContain(destination);
    } else {
      expect(await screen.findByTestId("assistant-page")).toBeTruthy();
    }
  });

  it("sends an inactive legacy /chat route to Assistant", async () => {
    renderAt("/chat");
    expect(await screen.findByTestId("assistant-page")).toBeTruthy();
  });

  it("redirects the old Assistant agent URL to the dedicated Assistant page", async () => {
    renderAt("/agents/assistant");
    expect(await screen.findByTestId("assistant-page")).toBeTruthy();
  });

  it("redirects the old schedules URL to Automations", async () => {
    renderAt("/schedules");
    expect(await screen.findByTestId("automations-page")).toBeTruthy();
  });

  it("redirects the old Functions URL to Settings / Functions", async () => {
    renderAt("/functions");
    expect(await screen.findByTestId("functions-settings-page")).toBeTruthy();
  });

  it("redirects an old run detail URL to the matching Activity detail", async () => {
    renderAt("/agent-runs/123");
    expect((await screen.findByTestId("activity-route")).textContent).toBe("/activity/runs/123");
  });
});
