// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import { integrations } from "../features/integrations/IntegrationNavigation";
import IntegrationsPage from "./IntegrationsPage";

beforeEach(() => {
  for (const method of ["getGitHubConnection", "getOutlookConnection", "getNotionConnection", "getQuercusConnection", "getGoogleCalendarConnection", "getGmailConnection", "getGoogleDriveConnection", "getTelegramConnection", "getTelegramAgentConnection", "getTelegramObserverAgentConnection", "getTelegramAssistantAgentConnection", "getWeComConnection", "getAtlasStatus", "getCodexMcpStatus", "getBrowserAuthentication", "getApiKeyConnection"] as const) {
    vi.spyOn(api, method).mockRejectedValue(new Error("Backend unavailable"));
  }
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.history.replaceState({}, "", "/");
});

function LocationDisplay() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname}</output>;
}

function renderIntegrations(path = "/settings/integrations") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/settings/integrations" element={<IntegrationsPage />} />
        <Route path="/settings/integrations/:integrationId" element={<IntegrationsPage />} />
      </Routes>
      <LocationDisplay />
    </MemoryRouter>,
  );
}

const githubStatus = {
  provider: "github" as const, connected: false, status: "disconnected" as const,
  account_login: null, account_id: null, last_validated_at: null,
  created_at: null, updated_at: null, error_type: null,
};

describe("Integration navigation", () => {
  it("shows every integration as a named link, loads status metadata, and keeps setup forms out of the overview", async () => {
    const github = vi.spyOn(api, "getGitHubConnection");
    const google = vi.spyOn(api, "getGoogleOAuthClient");
    const quercus = vi.spyOn(api, "getQuercusProcessing");
    const { container } = renderIntegrations();
    for (const integration of integrations) {
      expect(screen.getByRole("link", { name: integration.title }).getAttribute("href")).toBe(`/settings/integrations/${integration.id}`);
    }
    await waitFor(() => expect(screen.queryByText(/Loading integrations/)).toBeNull());
    expect(screen.getByRole("searchbox", { name: "Search integrations" })).toBeTruthy();
    expect(container.querySelector('input[type="password"]')).toBeNull();
    expect(github).toHaveBeenCalledTimes(1);
    expect(google).not.toHaveBeenCalled();
    expect(quercus).not.toHaveBeenCalled();
  });

  it("combines directory search and connection filters and clears an empty result", async () => {
    vi.spyOn(api, "getGitHubConnection").mockResolvedValue({ ...githubStatus, connected: true, status: "connected" });
    renderIntegrations();
    await waitFor(() => expect(screen.getByRole("link", { name: "GitHub" }).querySelector(".integration-connection-state")?.textContent).toContain("Connected"));

    fireEvent.change(screen.getByRole("searchbox", { name: "Search integrations" }), { target: { value: "repositories" } });
    expect(screen.getByRole("link", { name: "GitHub" })).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Google" })).toBeNull();
    fireEvent.change(screen.getByRole("combobox", { name: "Connection status" }), { target: { value: "disconnected" } });
    expect(screen.getByRole("heading", { name: "No integrations found" })).toBeTruthy();
    expect(screen.queryByRole("link", { name: "GitHub" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(screen.getByRole("link", { name: "GitHub" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Google" })).toBeTruthy();
    expect((screen.getByRole("searchbox", { name: "Search integrations" }) as HTMLInputElement).value).toBe("");
  });

  it("opens a focused provider page, keeps Integrations selected, and returns to the directory", async () => {
    const github = vi.spyOn(api, "getGitHubConnection").mockResolvedValue(githubStatus);
    const google = vi.spyOn(api, "getGoogleOAuthClient");
    const processing = vi.spyOn(api, "getQuercusProcessing");
    renderIntegrations();
    await waitFor(() => expect((screen.getByRole("button", { name: "Refresh" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("link", { name: "GitHub" }));
    expect(await screen.findByRole("heading", { name: "GitHub connection" })).toBeTruthy();
    expect(screen.getByTestId("location").textContent).toBe("/settings/integrations/github");
    expect(screen.getByRole("link", { name: "Integrations" }).getAttribute("aria-current")).toBe("page");
    expect(screen.queryByRole("heading", { name: "Google connection" })).toBeNull();
    expect(google).not.toHaveBeenCalled();
    expect(processing).not.toHaveBeenCalled();
    expect(github).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(github).toHaveBeenCalledTimes(3));
    fireEvent.click(screen.getByRole("link", { name: /All integrations/ }));
    expect(await screen.findByRole("heading", { level: 1, name: "Integrations" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Google" })).toBeTruthy();
  });

  it("shows live connection states independently and refreshes them", async () => {
    const github = vi.spyOn(api, "getGitHubConnection").mockResolvedValue({ ...githubStatus, connected: true, status: "connected" });
    renderIntegrations();
    await waitFor(() => expect(screen.getByRole("link", { name: "GitHub" }).querySelector(".integration-connection-state")?.textContent).toContain("Connected"));
    expect(screen.getByRole("link", { name: "GitHub" }).getAttribute("aria-describedby")).toBe("github-connection-status");
    expect(screen.getByRole("link", { name: "Google" }).querySelector(".integration-connection-state")?.textContent).toContain("Unavailable");
    github.mockResolvedValue(githubStatus);
    await waitFor(() => expect((screen.getByRole("button", { name: "Refresh" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(screen.getByRole("link", { name: "GitHub" }).querySelector(".integration-connection-state")?.textContent).toContain("Disconnected"));
  });

  it("shows Google as partial until all three services are connected", async () => {
    const fields = { connected: true, status: "connected" as const, account_email: null, last_validated_at: null, created_at: null, updated_at: null, error_type: null, oauth_redirect_uri: "" };
    vi.spyOn(api, "getGoogleCalendarConnection").mockResolvedValue({ ...fields, provider: "google_calendar" });
    vi.spyOn(api, "getGmailConnection").mockResolvedValue({ ...fields, provider: "gmail" });
    const drive = vi.spyOn(api, "getGoogleDriveConnection").mockResolvedValue({ ...fields, provider: "google_drive", connected: false, status: "disconnected" });
    renderIntegrations();
    await waitFor(() => expect(screen.getByRole("link", { name: "Google" }).querySelector(".integration-connection-state")?.textContent).toContain("Partial"));
    expect(screen.getByRole("link", { name: "Google" }).querySelector(".integration-connection-state")?.getAttribute("title")).toContain("Drive: disconnected");
    drive.mockResolvedValue({ ...fields, provider: "google_drive" });
    await waitFor(() => expect((screen.getByRole("button", { name: "Refresh" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(screen.getByRole("link", { name: "Google" }).querySelector(".integration-connection-state")?.textContent).toContain("Connected"));
  });

  it("supports direct links and contains provider failures on the selected page", async () => {
    vi.spyOn(api, "getGitHubConnection").mockRejectedValue(new Error("Backend unavailable"));
    renderIntegrations("/settings/integrations/github");
    expect(screen.getByRole("heading", { level: 1, name: "GitHub" })).toBeTruthy();
    expect(await screen.findByText("Backend unavailable")).toBeTruthy();
    expect(screen.getByRole("link", { name: /All integrations/ })).toBeTruthy();
  });

  it("discards an unsaved credential when leaving and returning to a provider", async () => {
    vi.spyOn(api, "getGitHubConnection").mockResolvedValue(githubStatus);
    renderIntegrations("/settings/integrations/github");
    const token = await screen.findByLabelText("GitHub token");
    fireEvent.change(token, { target: { value: "unsaved-test-token" } });
    fireEvent.click(screen.getByRole("link", { name: /All integrations/ }));
    fireEvent.click(await screen.findByRole("link", { name: "GitHub" }));
    expect((await screen.findByLabelText("GitHub token") as HTMLInputElement).value).toBe("");
  });

  it("returns unknown integrations to the overview", async () => {
    renderIntegrations("/settings/integrations/unknown");
    expect(await screen.findByRole("heading", { level: 1, name: "Integrations" })).toBeTruthy();
    expect(screen.getByTestId("location").textContent).toBe("/settings/integrations");
  });

  it.each([
    ["google_calendar", "google", "Google Calendar authorization was denied."],
    ["gmail", "google", "Gmail authorization was denied."],
    ["google_drive", "google", "Google Drive authorization was denied."],
    ["outlook", "microsoft", "Outlook authorization was denied."],
  ])("returns a legacy %s OAuth callback to its detail page and displays the result", async (marker, provider, message) => {
    const unavailable = () => Promise.reject(new Error("not configured"));
    vi.spyOn(api, "getGoogleOAuthClient").mockImplementation(unavailable);
    vi.spyOn(api, "getGoogleCalendarConnection").mockImplementation(unavailable);
    vi.spyOn(api, "getGmailConnection").mockImplementation(unavailable);
    vi.spyOn(api, "getGoogleDriveConnection").mockImplementation(unavailable);
    vi.spyOn(api, "getMicrosoftOAuthClient").mockImplementation(unavailable);
    vi.spyOn(api, "getOutlookConnection").mockImplementation(unavailable);
    const path = `/settings/integrations?${marker}=denied`;
    window.history.replaceState({}, "", path);
    renderIntegrations(path);
    expect(await screen.findByText(message)).toBeTruthy();
    expect(screen.getByTestId("location").textContent).toBe(`/settings/integrations/${provider}`);
    expect(window.location.search).toBe("");
  });
});
