// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import ApprovalRequestsPage from "./ApprovalRequestsPage";
import UsageSettingsPage, { SettingsSection } from "./UsageSettingsPage";

const permissionPolicy = {
  source: "backend/app/static/default_permissions.json",
  default_allowed: { runtime: { python_standard_library: true } },
  requires_approval: { runtime: { network: [] } },
  blocked: ["Shell, subprocess, and arbitrary command execution."],
  web_app: { supported: ["scripts"], blocked: ["popups"] },
};

beforeEach(() => {
  const unavailable = () => Promise.reject(new Error("not configured"));
  vi.spyOn(api, "getGitHubConnection").mockImplementation(unavailable);
  vi.spyOn(api, "getAtlasStatus").mockImplementation(unavailable);
  vi.spyOn(api, "getNotionConnection").mockImplementation(unavailable);
  vi.spyOn(api, "getQuercusConnection").mockImplementation(unavailable);
  vi.spyOn(api, "getQuercusProcessing").mockResolvedValue({
    method: "none",
    llama_cpp_directory: null,
    llama_cpp_available: false,
    inference_url: "http://127.0.0.1:8081/v1",
    marker_available: false,
    inference_available: false,
    status: "disabled",
    processed_file_count: 0,
    failed_file_count: 0,
  });
  vi.spyOn(api, "getGoogleOAuthClient").mockImplementation(unavailable);
  vi.spyOn(api, "getGoogleCalendarConnection").mockImplementation(unavailable);
  vi.spyOn(api, "getGmailConnection").mockImplementation(unavailable);
  vi.spyOn(api, "getTelegramConnection").mockImplementation(unavailable);
  vi.spyOn(api, "getTelegramAgentConnection").mockImplementation(unavailable);
  vi.spyOn(api, "getCodexMcpStatus").mockImplementation(unavailable);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function renderSettings(section: SettingsSection) {
  return render(
    <MemoryRouter initialEntries={[`/settings/${section}`]}>
      <UsageSettingsPage section={section} />
    </MemoryRouter>,
  );
}

describe("Settings subpages", () => {
  it("shows focused navigation and loads only the selected section", async () => {
    const usageRequest = vi.spyOn(api, "getCodexUsage");
    vi.spyOn(api, "getPermissionPolicy").mockResolvedValue(permissionPolicy);

    renderSettings("permissions");

    expect(screen.getByRole("heading", { level: 1, name: "Permissions" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Permissions" }).getAttribute("aria-current")).toBe("page");
    expect(screen.getByRole("link", { name: "Integrations" }).getAttribute("href")).toBe("/settings/integrations");
    expect(await screen.findByRole("heading", { name: "Permission policy" })).toBeTruthy();
    expect(screen.getByText("Shell, subprocess, and arbitrary command execution.")).toBeTruthy();
    expect(document.body.textContent).toContain("python_standard_library");
    expect(usageRequest).not.toHaveBeenCalled();
  });
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
      act: choice,
      product_manager: {
        default: choice,
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

    renderSettings("integrations");
    const input = await screen.findByLabelText("GitHub token");
    fireEvent.change(input, { target: { value: sentinel } });
    fireEvent.click(screen.getByRole("button", { name: "Add connection" }));

    await waitFor(() => expect(put).toHaveBeenCalledWith(sentinel));
    await waitFor(() => expect(screen.getByText("octocat")).toBeTruthy());
    expect((input as HTMLInputElement).value).toBe("");
    expect(document.body.textContent).not.toContain(sentinel);
  });
});

describe("Quercus Settings", () => {
  it("polls only while Quercus processing is pending or running", async () => {
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
    const interval = vi.spyOn(window, "setInterval");
    const processing = vi.spyOn(api, "getQuercusProcessing");

    renderSettings("integrations");

    await screen.findByRole("heading", { name: "File processing" });
    expect(processing).toHaveBeenCalledTimes(1);
    expect(interval).not.toHaveBeenCalledWith(expect.any(Function), 3000);

    cleanup();
    processing.mockResolvedValue({
      method: "marker_surya_llamacpp",
      llama_cpp_directory: "C:\\Tools\\llama-cpp-cuda",
      llama_cpp_available: true,
      inference_url: "http://127.0.0.1:8081/v1",
      marker_available: true,
      inference_available: true,
      status: "running",
      processed_file_count: 42,
      failed_file_count: 0,
    });

    renderSettings("integrations");

    await waitFor(() => expect(interval).toHaveBeenCalledWith(expect.any(Function), 3000));
  });

  it("shows sync state and saves the exact selected course set", async () => {
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
    const course = {
      course_id: "course-1",
      name: "Distributed Systems",
      course_code: "CSC400",
      term_name: "Fall 2026",
      enrollment_state: "active",
      selected: false,
      retained: true,
      local_path: "knowledge/quercus/CSC400 - Distributed Systems",
      last_sync_started_at: null,
      last_sync_completed_at: "2026-09-03T14:05:00Z",
      last_sync_status: "partial",
      last_error_type: "file",
      skipped_file_count: 2,
      last_processing_started_at: null,
      last_processing_completed_at: "2026-09-03T14:06:00Z",
      last_processing_status: "partial",
      last_processing_error_type: "marker_failed",
      processed_file_count: 10,
      failed_processing_count: 1,
    };
    vi.spyOn(api, "getQuercusConnection").mockResolvedValue({
      provider: "quercus",
      connected: true,
      status: "connected",
      account_name: "Student",
      account_id: "user-1",
      last_validated_at: "2026-09-03T14:00:00Z",
      created_at: "2026-09-03T14:00:00Z",
      updated_at: "2026-09-03T14:00:00Z",
      error_type: null,
      courses: [course],
    });
    vi.spyOn(api, "getQuercusCourses").mockResolvedValue([course]);
    const save = vi.spyOn(api, "putQuercusCourses").mockResolvedValue([
      { ...course, selected: true, retained: false },
    ]);
    const remove = vi.spyOn(api, "deleteQuercusCourse").mockResolvedValue(undefined);

    const { container } = renderSettings("integrations");

    const coursesHeading = await screen.findByRole("heading", { name: "Courses" });
    const disclosure = coursesHeading.closest("details");
    expect(disclosure?.hasAttribute("open")).toBe(false);
    expect(disclosure?.querySelector(".disclosure-chevron")).toBeTruthy();
    fireEvent.click(coursesHeading.closest("summary")!);
    expect(disclosure?.hasAttribute("open")).toBe(true);
    expect(container.querySelector(".quercus-course-disclosure[open] .disclosure-chevron")).toBeTruthy();
    const checkbox = screen.getByRole("checkbox", { name: /Distributed Systems/ });
    expect(screen.getByText(/2 files skipped/)).toBeTruthy();
    expect(screen.getByText(/Processing: partial/)).toBeTruthy();
    fireEvent.click(checkbox);
    fireEvent.click(screen.getByRole("button", { name: "Save course selection" }));

    await waitFor(() => expect(save).toHaveBeenCalledWith(["course-1"]));
    expect(await screen.findByText(/background sync was queued/i)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Delete Distributed Systems" }));
    expect(remove).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Remove Distributed Systems?" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Permanently remove course" }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith("course-1"));
    expect(await screen.findByText(/permanently removed from the course list/i)).toBeTruthy();
    expect(screen.queryByRole("checkbox", { name: /Distributed Systems/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete Distributed Systems" })).toBeNull();
  });

  it("shows delete for an untracked Quercus course and removes it from the list", async () => {
    vi.spyOn(api, "getGitHubConnection").mockResolvedValue({
      provider: "github", connected: false, status: "disconnected", account_login: null,
      account_id: null, last_validated_at: null, created_at: null, updated_at: null, error_type: null,
    });
    const course = {
      course_id: "course-2", name: "Compilers", course_code: "CSC488", term_name: "Fall 2026",
      enrollment_state: "active", selected: false, retained: false, local_path: null,
      last_sync_started_at: null, last_sync_completed_at: null, last_sync_status: null,
      last_error_type: null, skipped_file_count: 0, last_processing_started_at: null,
      last_processing_completed_at: null, last_processing_status: null,
      last_processing_error_type: null, processed_file_count: 0, failed_processing_count: 0,
    };
    const otherCourse = {
      ...course, course_id: "course-3", name: "Algorithms", course_code: "CSC373",
    };
    vi.spyOn(api, "getQuercusConnection").mockResolvedValue({
      provider: "quercus", connected: true, status: "connected", account_name: "Student",
      account_id: "user-1", last_validated_at: "2026-09-03T14:00:00Z",
      created_at: "2026-09-03T14:00:00Z", updated_at: "2026-09-03T14:00:00Z",
      error_type: null, courses: [],
    });
    vi.spyOn(api, "getQuercusCourses").mockResolvedValue([course, otherCourse]);
    let finishDelete: (() => void) | undefined;
    const deletion = new Promise<void>((resolve) => { finishDelete = resolve; });
    const remove = vi.spyOn(api, "deleteQuercusCourse").mockReturnValue(deletion);

    renderSettings("integrations");
    const coursesHeading = await screen.findByRole("heading", { name: "Courses" });
    fireEvent.click(coursesHeading.closest("summary")!);
    fireEvent.click(screen.getByRole("button", { name: "Delete Compilers" }));
    expect(remove).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Permanently remove course" }));

    await waitFor(() => expect(remove).toHaveBeenCalledWith("course-2"));
    expect((screen.getByRole("button", { name: "Delete Algorithms" }) as HTMLButtonElement).disabled).toBe(false);
    finishDelete?.();
    await waitFor(() => expect(screen.queryByRole("checkbox", { name: /Compilers/ })).toBeNull());
  });

  it("saves Marker processing and reports local readiness", async () => {
    vi.spyOn(api, "getGitHubConnection").mockResolvedValue({
      provider: "github", connected: false, status: "disconnected", account_login: null,
      account_id: null, last_validated_at: null, created_at: null, updated_at: null, error_type: null,
    });
    vi.spyOn(api, "getQuercusConnection").mockResolvedValue({
      provider: "quercus", connected: false, status: "disconnected", account_name: null,
      account_id: null, last_validated_at: null, created_at: null, updated_at: null,
      error_type: null, courses: [],
    });
    vi.spyOn(api, "getQuercusProcessing").mockResolvedValue({
      method: "none", llama_cpp_directory: "C:\\Tools\\llama-cpp-cuda",
      llama_cpp_available: true, inference_url: "http://127.0.0.1:8081/v1", marker_available: true,
      inference_available: false, status: "disabled", processed_file_count: 0, failed_file_count: 0,
    });
    const save = vi.spyOn(api, "putQuercusProcessing").mockResolvedValue({
      method: "marker_surya_llamacpp", llama_cpp_directory: "C:\\Tools\\llama-cpp-cuda",
      llama_cpp_available: true, inference_url: "http://127.0.0.1:8081/v1",
      marker_available: true, inference_available: false, status: "pending",
      processed_file_count: 0, failed_file_count: 0,
    });

    renderSettings("integrations");
    const selector = await screen.findByRole("combobox", { name: "Processing method" });
    fireEvent.change(selector, { target: { value: "marker_surya_llamacpp" } });
    fireEvent.click(screen.getByRole("button", { name: "Save file processing" }));

    await waitFor(() => expect(save).toHaveBeenCalledWith(
      "marker_surya_llamacpp",
      "C:\\Tools\\llama-cpp-cuda",
    ));
    expect(await screen.findByText(/background backfill was queued/i)).toBeTruthy();
    expect(screen.getByText("Starts on demand", { selector: "dd" })).toBeTruthy();
  });

  it("queues failed Quercus files only through the explicit action", async () => {
    vi.spyOn(api, "getGitHubConnection").mockResolvedValue({
      provider: "github", connected: false, status: "disconnected", account_login: null,
      account_id: null, last_validated_at: null, created_at: null, updated_at: null,
      error_type: null,
    });
    vi.spyOn(api, "getQuercusConnection").mockResolvedValue({
      provider: "quercus", connected: false, status: "disconnected", account_name: null,
      account_id: null, last_validated_at: null, created_at: null, updated_at: null,
      error_type: null, courses: [],
    });
    vi.spyOn(api, "getQuercusProcessing").mockResolvedValue({
      method: "marker_surya_llamacpp", llama_cpp_directory: "C:\\Tools\\llama-cpp-cuda",
      llama_cpp_available: true, inference_url: null, marker_available: true,
      inference_available: false, status: "partial", processed_file_count: 45,
      failed_file_count: 3,
    });
    const reprocess = vi.spyOn(api, "reprocessFailedQuercusFiles").mockResolvedValue({
      method: "marker_surya_llamacpp", llama_cpp_directory: "C:\\Tools\\llama-cpp-cuda",
      llama_cpp_available: true, inference_url: null, marker_available: true,
      inference_available: false, status: "pending", processed_file_count: 45,
      failed_file_count: 0, queued_file_count: 3,
    });

    renderSettings("integrations");
    const button = await screen.findByRole("button", { name: "Reprocess failed files" });
    expect((button as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(button);

    await waitFor(() => expect(reprocess).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("3 failed Quercus files were queued for reprocessing.")).toBeTruthy();
  });
});

describe("Notion Settings connection", () => {
  it("saves the credential first and manages both data-source IDs together", async () => {
    const sentinel = "EIDOLON_NOTION_UI_SENTINEL_b411";
    const choice = { model: null, reasoning_effort: null };
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
    vi.spyOn(api, "getCodexRoutingSettings").mockResolvedValue({
      project_build_workflow_override: null,
      act: choice,
      product_manager: {
        default: choice,
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
      tester: { default: choice, task: choice, final_e2e: choice, update: choice },
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
    vi.spyOn(api, "getAtlasStatus").mockRejectedValue(new Error("not running"));
    vi.spyOn(api, "getNotionConnection").mockResolvedValue({
      provider: "notion",
      connected: false,
      status: "disconnected",
      bot_name: null,
      bot_id: null,
      workspace_name: null,
      data_source_id: null,
      report_data_source_id: null,
      last_validated_at: null,
      created_at: null,
      updated_at: null,
      error_type: null,
    });
    const put = vi.spyOn(api, "putNotionConnection").mockResolvedValue({
      provider: "notion",
      connected: true,
      status: "connected",
      bot_name: "Eidolon Todo",
      bot_id: "bot-id",
      workspace_name: "Private workspace",
      data_source_id: null,
      report_data_source_id: null,
      last_validated_at: "2026-01-01T00:00:00Z",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      error_type: null,
    });
    const putDataSources = vi.spyOn(api, "putNotionDataSources").mockResolvedValue({
      provider: "notion",
      connected: true,
      status: "connected",
      bot_name: "Eidolon Todo",
      bot_id: "bot-id",
      workspace_name: "Private workspace",
      data_source_id: "source-id",
      report_data_source_id: "report-source-id",
      last_validated_at: "2026-01-01T00:00:00Z",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      error_type: null,
    });
    const removeDataSources = vi.spyOn(api, "removeNotionDataSources").mockResolvedValue({
      provider: "notion",
      connected: true,
      status: "connected",
      bot_name: "Eidolon Todo",
      bot_id: "bot-id",
      workspace_name: "Private workspace",
      data_source_id: null,
      report_data_source_id: null,
      last_validated_at: "2026-01-01T00:00:00Z",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      error_type: null,
    });

    renderSettings("integrations");
    const token = await screen.findByLabelText("Notion token");
    const todoSource = screen.getByLabelText("Notion Todo data-source ID") as HTMLInputElement;
    expect(todoSource.disabled).toBe(true);
    expect(token.compareDocumentPosition(todoSource) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fireEvent.change(token, { target: { value: sentinel } });
    fireEvent.click(screen.getByRole("button", { name: "Add Notion connection" }));

    await waitFor(() => expect(put).toHaveBeenCalledWith(sentinel));
    await waitFor(() => expect(screen.getByText("Private workspace")).toBeTruthy());
    expect((screen.getByLabelText("Replacement Notion token") as HTMLInputElement).value).toBe("");
    fireEvent.change(screen.getByLabelText("Notion Todo data-source ID"), {
      target: { value: "source-id" },
    });
    fireEvent.change(screen.getByLabelText("Notion Reports data-source ID"), {
      target: { value: "report-source-id" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save data-source IDs" }));
    await waitFor(() => expect(putDataSources).toHaveBeenCalledWith("source-id", "report-source-id"));
    fireEvent.click(screen.getByRole("button", { name: "Delete data-source IDs" }));
    await waitFor(() => expect(removeDataSources).toHaveBeenCalledOnce());
    expect(document.body.textContent).not.toContain(sentinel);
    expect(screen.queryByRole("button", { name: /create todo/i })).toBeNull();
    expect(screen.getByText(/Eidolon does not keep a todo copy/)).toBeTruthy();
  });
});

describe("Google Calendar OAuth connection", () => {
  it("shows the fixed callback and never exposes OAuth client inputs as plain text", async () => {
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
    vi.spyOn(api, "getGoogleCalendarConnection").mockResolvedValue({
      provider: "google_calendar",
      connected: true,
      status: "connected",
      account_email: "person@example.com",
      last_validated_at: "2026-08-29T12:00:00Z",
      created_at: "2026-08-29T12:00:00Z",
      updated_at: "2026-08-29T12:00:00Z",
      error_type: null,
      oauth_redirect_uri: "http://localhost:8000/settings/integrations/google-calendar/oauth/callback",
    });
    vi.spyOn(api, "getGoogleOAuthClient").mockResolvedValue({
      provider: "google",
      configured: true,
      status: "configured",
      calendar_redirect_uri: "http://localhost:8000/settings/integrations/google-calendar/oauth/callback",
      gmail_redirect_uri: "http://localhost:8000/settings/integrations/gmail/oauth/callback",
      created_at: "2026-08-29T12:00:00Z",
      updated_at: "2026-08-29T12:00:00Z",
      error_type: null,
    });
    vi.spyOn(api, "getGmailConnection").mockResolvedValue({
      provider: "gmail",
      connected: true,
      status: "connected",
      account_email: "mail@example.com",
      last_validated_at: "2026-08-29T12:00:00Z",
      created_at: "2026-08-29T12:00:00Z",
      updated_at: "2026-08-29T12:00:00Z",
      error_type: null,
      oauth_redirect_uri: "http://localhost:8000/settings/integrations/gmail/oauth/callback",
    });
    vi.spyOn(api, "getTelegramConnection").mockResolvedValue({
      provider: "telegram",
      connected: true,
      status: "connected",
      bot_username: "eidolon_bot",
      paired_chat_id: "11",
      paired_user_id: "22",
      pairing_expires_at: null,
      last_validated_at: "2026-08-29T12:00:00Z",
      created_at: "2026-08-29T12:00:00Z",
      updated_at: "2026-08-29T12:00:00Z",
      error_type: null,
    });
    vi.spyOn(api, "getTelegramAgentConnection").mockResolvedValue({
      provider: "telegram",
      connected: false,
      status: "disconnected",
      bot_username: null,
      paired_chat_id: null,
      paired_user_id: null,
      pairing_expires_at: null,
      last_validated_at: null,
      created_at: null,
      updated_at: null,
      error_type: null,
    });
    vi.spyOn(api, "getAtlasStatus").mockRejectedValue(new Error("not running"));
    vi.spyOn(api, "getNotionConnection").mockRejectedValue(new Error("not connected"));
    vi.spyOn(api, "getCodexMcpStatus").mockRejectedValue(new Error("not installed"));

    renderSettings("integrations");

    expect(await screen.findByRole("heading", { name: "Google connection" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Google Calendar" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Gmail" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Telegram" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Notification / Approval bot" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Agent bot" })).toBeTruthy();
    expect(screen.getByLabelText("Replacement Notification/Approval bot token")).toBeTruthy();
    expect(screen.getByLabelText("Agent bot token")).toBeTruthy();
    expect(screen.getByText("person@example.com")).toBeTruthy();
    expect(screen.getByText("mail@example.com")).toBeTruthy();
    expect(
      screen.getByText("http://localhost:8000/settings/integrations/google-calendar/oauth/callback"),
    ).toBeTruthy();
    expect(screen.getByText("http://localhost:8000/settings/integrations/gmail/oauth/callback")).toBeTruthy();
    expect((screen.getByLabelText("Replacement Google OAuth client ID") as HTMLInputElement).type).toBe(
      "password",
    );
    expect(
      (screen.getByLabelText("Replacement Google OAuth client secret") as HTMLInputElement).type,
    ).toBe("password");
    expect(screen.queryByLabelText("Gmail OAuth client ID")).toBeNull();
    expect(screen.getByRole("button", { name: "Choose another Calendar account" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Choose another Gmail account" })).toBeTruthy();

    vi.spyOn(api, "listPermissionRequests").mockResolvedValue([]);
    vi.spyOn(api, "listInvocationApprovals").mockResolvedValue([]);
    cleanup();
    render(<ApprovalRequestsPage />);
    fireEvent.click(await screen.findByRole("tab", { name: "Invocation approvals" }));
    expect(screen.getByRole("heading", { name: "Actions" })).toBeTruthy();
    expect(screen.getByText("No invocation approvals yet.")).toBeTruthy();
  });
});

describe("Codex tools registration", () => {
  it("installs, repairs, and removes the owned MCP registration", async () => {
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
    vi.spyOn(api, "getAtlasStatus").mockRejectedValue(new Error("not running"));
    vi.spyOn(api, "getNotionConnection").mockRejectedValue(new Error("not connected"));
    const unregistered = {
      enabled: false,
      registered: false,
      config_matches: false,
      available_tool_count: 9,
      excluded_ids: ["backend.codex.call"],
      config_path: "C:\\Users\\tester\\.codex\\config.toml",
      restart_required: false,
      error_type: null,
      error: null,
    };
    const installed = {
      ...unregistered,
      enabled: true,
      registered: true,
      config_matches: true,
      restart_required: true,
    };
    vi.spyOn(api, "getCodexMcpStatus").mockResolvedValue(unregistered);
    const update = vi.spyOn(api, "updateCodexMcp").mockResolvedValue(installed);
    const remove = vi.spyOn(api, "removeCodexMcp").mockResolvedValue(unregistered);

    renderSettings("integrations");
    expect(await screen.findByRole("heading", { name: "Codex tools" })).toBeTruthy();
    expect(screen.getByText("backend.codex.call")).toBeTruthy();
    expect(screen.getByText(/Open sessions are not hot-refreshed/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Install" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith("install"));
    expect(await screen.findByRole("button", { name: "Repair" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Repair" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith("repair"));
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(remove).toHaveBeenCalledTimes(1));
    expect(await screen.findByRole("button", { name: "Install" })).toBeTruthy();
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
      act: choice,
      product_manager: {
        default: choice,
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

    renderSettings("models");
    expect(await screen.findByLabelText("Project planning and clarification model")).toBeTruthy();
    expect(screen.queryByText("Plausibility review")).toBeNull();
    fireEvent.change(await screen.findByLabelText("Single Codex model"), {
      target: { value: "gpt-smart" },
    });
    fireEvent.change(screen.getByLabelText("Single Codex effort"), {
      target: { value: "xhigh" },
    });
    expect(screen.getAllByText("Unsaved Codex routing changes.").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "Save model routing" }));

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith(
        expect.objectContaining({
          builder: expect.objectContaining({
            single_codex: { model: "gpt-smart", reasoning_effort: "xhigh" },
          }),
        }),
      ),
    );
    await waitFor(() => expect(screen.getAllByText(/Future invocations use these routes/).length).toBeGreaterThan(0));
  });
});

describe("Atlas Settings", () => {
  it("uses passphrase-only owned-process controls and no API-key UI", async () => {
    const passphraseSentinel = "EIDOLON_ATLAS_PASSPHRASE_UI_SENTINEL_8b2e";
    const choice = { model: null, reasoning_effort: null };
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
    vi.spyOn(api, "getCodexRoutingSettings").mockResolvedValue({
      project_build_workflow_override: null,
      act: choice,
      product_manager: {
        default: choice,
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
    const initialAtlas = {
      provider: "atlas" as const,
      directory: "C:\\Users\\John\\Projects\\Eidolon-Atlas",
      process_ownership: "owned" as const,
      running: true,
      initialized: true,
      locked: true,
      passphrase_configured: false,
      startup_error: null,
      error_type: null,
    };
    const configuredAtlas = { ...initialAtlas, passphrase_configured: true, locked: false };
    const externalAtlas = {
      ...configuredAtlas,
      process_ownership: "external" as const,
      locked: true,
    };
    vi.spyOn(api, "getAtlasStatus").mockResolvedValue(initialAtlas);
    const putPassphrase = vi.spyOn(api, "putAtlasPassphrase").mockResolvedValue(configuredAtlas);
    const unlock = vi.spyOn(api, "unlockAtlas").mockResolvedValue(configuredAtlas);
    vi.spyOn(api, "restartAtlas").mockResolvedValue(externalAtlas);

    renderSettings("integrations");
    expect(await screen.findByText("Running (Eidolon-owned)")).toBeTruthy();
    expect(screen.getByText(/Storing the passphrase shifts practical at-rest protection to your Windows account/)).toBeTruthy();

    expect(screen.queryByLabelText(/API key/i)).toBeNull();
    expect(screen.queryByRole("button", { name: /API key/i })).toBeNull();

    const passphraseInput = screen.getByLabelText("Atlas passphrase");
    fireEvent.change(passphraseInput, { target: { value: passphraseSentinel } });
    fireEvent.click(screen.getByRole("button", { name: "Store passphrase" }));
    await waitFor(() => expect(putPassphrase).toHaveBeenCalledWith(passphraseSentinel));
    await waitFor(() => expect(screen.getByLabelText("Replacement Atlas passphrase")).toBeTruthy());
    expect((screen.getByLabelText("Replacement Atlas passphrase") as HTMLInputElement).value).toBe("");
    expect(document.body.textContent).not.toContain(passphraseSentinel);

    fireEvent.click(screen.getByRole("button", { name: "Unlock now" }));
    await waitFor(() => expect(unlock).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: "Restart Atlas" }));
    expect(await screen.findByText(/Unlock it through Atlas itself/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Unlock now" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("button", { name: "Replace passphrase" }).hasAttribute("disabled")).toBe(true);
  });
});
