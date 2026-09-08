// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FunctionCatalogEntry, api } from "../api/client";
import FunctionsPage, { FunctionTable } from "./FunctionsPage";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("FunctionsPage", () => {
  it("rebuilds the catalog when Refresh is clicked", async () => {
    const listFunctionCatalog = vi.spyOn(api, "listFunctionCatalog").mockResolvedValue([]);

    render(<MemoryRouter><FunctionsPage /></MemoryRouter>);

    const refresh = await screen.findByRole("button", { name: "Refresh" });
    fireEvent.click(refresh);
    await waitFor(() => expect(listFunctionCatalog).toHaveBeenCalledWith(true));
  });
});

describe("FunctionTable", () => {
  it("shows sources, states, reasons, and user-skill links", () => {
    const functions: FunctionCatalogEntry[] = [
      {
        id: "normalize_text",
        category: "user",
        title: "normalize_text",
        description: "Normalizes whitespace.",
        risk_level: "low",
        input_schema: { type: "object" },
        output_schema: { type: "object" },
        availability: "disabled",
        availability_reasons: ["Skill is disabled"],
        invocation: {},
        call_name: "normalize_text",
        provider: null,
        skill_id: 12,
        active_version: "v1",
        is_running: true,
      },
      {
        id: "github.repository.get",
        category: "integration",
        title: "Read repository",
        description: "Reads repository metadata.",
        risk_level: "medium",
        input_schema: { type: "object" },
        output_schema: { type: "object" },
        availability: "unavailable",
        availability_reasons: ["GitHub connection is not configured"],
        invocation: {},
        call_name: null,
        provider: "github",
        skill_id: null,
        active_version: null,
        is_running: false,
      },
    ];

    render(
      <MemoryRouter>
        <FunctionTable functions={functions} />
      </MemoryRouter>,
    );

    expect(screen.getByRole("link", { name: "Normalize Text" }).getAttribute("href")).toBe("/skills/12");
    expect(screen.getByRole("cell", { name: "User" })).toBeTruthy();
    expect(screen.getByRole("cell", { name: "GitHub" })).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: "Source" })).toBeTruthy();
    const runningDot = screen.getByLabelText("Running");
    expect(runningDot.getAttribute("title")).toBe("Running");
    expect(runningDot.closest("td")?.cellIndex).toBe(2);
    expect(runningDot.closest(".running-state-status")?.textContent).toBe("running");
    expect(runningDot.closest("tr")?.firstElementChild?.querySelector(".running-state-dot")).toBeNull();
    expect(screen.getByText("Skill is disabled")).toBeTruthy();
    expect(screen.getByText("GitHub connection is not configured")).toBeTruthy();
  });

  it("combines keyword, source, availability, and risk filters", () => {
    const functions: FunctionCatalogEntry[] = [
      {
        id: "github.repository.get",
        category: "integration",
        title: "Read repository",
        description: "Reads repository metadata.",
        risk_level: "medium",
        input_schema: null,
        output_schema: null,
        availability: "unavailable",
        availability_reasons: [],
        invocation: {},
        call_name: null,
        provider: "github",
        skill_id: null,
        active_version: null,
        is_running: false,
      },
      {
        id: "github.issue.list",
        category: "integration",
        title: "List issues",
        description: "Lists repository issues.",
        risk_level: "high",
        input_schema: null,
        output_schema: null,
        availability: "available",
        availability_reasons: [],
        invocation: {},
        call_name: null,
        provider: "github",
        skill_id: null,
        active_version: null,
        is_running: false,
      },
      {
        id: "google_calendar.event.create",
        category: "integration",
        title: "Create event",
        description: "Creates a calendar event.",
        risk_level: "medium",
        input_schema: null,
        output_schema: null,
        availability: "available",
        availability_reasons: [],
        invocation: {},
        call_name: null,
        provider: "google_calendar",
        skill_id: null,
        active_version: null,
        is_running: false,
      },
    ];

    render(
      <MemoryRouter>
        <FunctionTable functions={functions} />
      </MemoryRouter>,
    );

    fireEvent.change(screen.getByLabelText("Search"), { target: { value: "calendar" } });
    expect(screen.getByText("google_calendar.event.create")).toBeTruthy();
    expect(screen.queryByText("github.repository.get")).toBeNull();

    fireEvent.change(screen.getByLabelText("Search"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Source"), { target: { value: "provider:github" } });
    expect(screen.getByText("github.repository.get")).toBeTruthy();
    expect(screen.getByText("github.issue.list")).toBeTruthy();
    expect(screen.queryByText("google_calendar.event.create")).toBeNull();

    fireEvent.change(screen.getByLabelText("Availability"), { target: { value: "available" } });
    expect(screen.queryByText("github.repository.get")).toBeNull();
    expect(screen.getByText("github.issue.list")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Risk"), { target: { value: "medium" } });
    expect(screen.getByText("No functions match these filters.")).toBeTruthy();
  });
});
