// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import UsageSettingsPage from "./UsageSettingsPage";

const permissionPolicy = {
  source: "backend/app/static/default_permissions.json",
  default_allowed: { runtime: { python_standard_library: true } },
  requires_approval: { runtime: { network: [] } },
  blocked: ["Shell, subprocess, and arbitrary command execution."],
  web_app: { supported: ["scripts"], blocked: ["popups"] },
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("GitHub Settings connection", () => {
  it("submits the token once and never renders it after validation", async () => {
    const sentinel = "EIDOLON_GITHUB_UI_SENTINEL_29df";
    vi.spyOn(api, "getCodexUsage").mockResolvedValue({
      available: false,
      source: "test",
      fetched_at: "2026-01-01T00:00:00Z",
      plan_type: null,
      limit_id: "test",
      rate_limit_reached_type: null,
      five_hour: null,
      weekly: null,
    });
    vi.spyOn(api, "getCodexCliStatus").mockResolvedValue({
      available: false,
      compatible: false,
      requested_command: null,
      explicit_override: false,
      resolved_path: null,
      source: null,
      version: null,
      minimum_version: null,
      error: null,
      candidates: [],
    });
    vi.spyOn(api, "getCodexModels").mockResolvedValue({
      available: false,
      fetched_at: "2026-01-01T00:00:00Z",
      error: null,
      models: [],
    });
    const choice = { model: null, reasoning_effort: null };
    vi.spyOn(api, "getCodexRoutingSettings").mockResolvedValue({
      project_build_workflow_override: null,
      chat: choice,
      product_manager: {
        default: choice,
        refine_intent: choice,
        plausibility_review: choice,
        blueprint_and_permissions: choice,
        task_dag: choice,
        repair: choice,
        update: choice,
      },
      builder: {
        default: choice,
        single_codex: choice,
        easy: choice,
        medium: choice,
        hard: choice,
        repair: choice,
        update: choice,
      },
      tester: {
        default: choice,
        task: choice,
        final_e2e: choice,
        update: choice,
      },
      updated_at: null,
    });
    vi.spyOn(api, "getPermissionPolicy").mockResolvedValue(permissionPolicy);
    vi.spyOn(api, "getGitHubConnection").mockResolvedValue({
      provider: "github",
      connected: false,
      status: "disconnected",
      account_login: null,
      account_id: null,
      last_validated_at: null,
      created_at: null,
      updated_at: null,
      error_type: null,
    });
    const put = vi.spyOn(api, "putGitHubConnection").mockResolvedValue({
      provider: "github",
      connected: true,
      status: "connected",
      account_login: "octocat",
      account_id: "1",
      last_validated_at: "2026-01-01T00:00:00Z",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      error_type: null,
    });

    render(<UsageSettingsPage />);
    const input = await screen.findByLabelText("GitHub token");
    expect(screen.getByRole("heading", { name: "Permission policy" })).toBeTruthy();
    expect(screen.getByText("Shell, subprocess, and arbitrary command execution.")).toBeTruthy();
    expect(document.body.textContent).toContain("python_standard_library");
    fireEvent.change(input, { target: { value: sentinel } });
    fireEvent.click(screen.getByRole("button", { name: "Add connection" }));

    await waitFor(() => expect(put).toHaveBeenCalledWith(sentinel));
    await waitFor(() => expect(screen.getByText("octocat")).toBeTruthy());
    expect((input as HTMLInputElement).value).toBe("");
    expect(document.body.textContent).not.toContain(sentinel);
  });
});

describe("Codex model routing", () => {
  it("saves an independent model and effort for the single Codex Builder", async () => {
    vi.spyOn(api, "getCodexUsage").mockResolvedValue({
      available: false,
      source: "test",
      fetched_at: "2026-01-01T00:00:00Z",
      plan_type: null,
      limit_id: "test",
      rate_limit_reached_type: null,
      five_hour: null,
      weekly: null,
    });
    vi.spyOn(api, "getCodexCliStatus").mockResolvedValue({
      available: true,
      compatible: true,
      requested_command: "codex",
      explicit_override: false,
      resolved_path: "codex",
      source: "path",
      version: "1.0.0",
      minimum_version: null,
      error: null,
      candidates: [],
    });
    vi.spyOn(api, "getCodexModels").mockResolvedValue({
      available: true,
      fetched_at: "2026-01-01T00:00:00Z",
      error: null,
      models: [
        {
          id: "gpt-fast",
          model: "gpt-fast",
          display_name: "GPT Fast",
          description: "Fast model",
          is_default: true,
          default_reasoning_effort: "medium",
          supported_reasoning_efforts: ["low", "medium"],
        },
        {
          id: "gpt-smart",
          model: "gpt-smart",
          display_name: "GPT Smart",
          description: "Smart model",
          is_default: false,
          default_reasoning_effort: "high",
          supported_reasoning_efforts: ["medium", "high", "xhigh"],
        },
      ],
    });
    const choice = { model: null, reasoning_effort: null };
    const routing = {
      project_build_workflow_override: null,
      chat: choice,
      product_manager: {
        default: choice,
        refine_intent: choice,
        plausibility_review: choice,
        blueprint_and_permissions: choice,
        task_dag: choice,
        repair: choice,
        update: choice,
      },
      builder: {
        default: choice,
        single_codex: choice,
        easy: choice,
        medium: choice,
        hard: choice,
        repair: choice,
        update: choice,
      },
      tester: {
        default: choice,
        task: choice,
        final_e2e: choice,
        update: choice,
      },
      updated_at: null,
    };
    vi.spyOn(api, "getCodexRoutingSettings").mockResolvedValue(routing);
    vi.spyOn(api, "getPermissionPolicy").mockResolvedValue(permissionPolicy);
    vi.spyOn(api, "getGitHubConnection").mockResolvedValue({
      provider: "github",
      connected: false,
      status: "disconnected",
      account_login: null,
      account_id: null,
      last_validated_at: null,
      created_at: null,
      updated_at: null,
      error_type: null,
    });
    const update = vi.spyOn(api, "updateCodexRoutingSettings").mockImplementation(async (payload) => ({
      ...payload,
      updated_at: "2026-01-01T00:00:00Z",
    }));

    render(<UsageSettingsPage />);
    fireEvent.change(await screen.findByLabelText("Single Codex model"), {
      target: { value: "gpt-smart" },
    });
    fireEvent.change(screen.getByLabelText("Single Codex effort"), {
      target: { value: "xhigh" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save Codex settings" }));

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith(
        expect.objectContaining({
          builder: expect.objectContaining({
            single_codex: { model: "gpt-smart", reasoning_effort: "xhigh" },
          }),
        }),
      ),
    );
  });
});
