// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { FunctionCatalogEntry } from "../api/client";
import { FunctionTable } from "./FunctionsPage";

describe("FunctionTable", () => {
  it("shows categories, states, reasons, and user-skill links", () => {
    const functions: FunctionCatalogEntry[] = [
      {
        id: "normalize_text",
        category: "user",
        title: "Normalize text",
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
      },
    ];

    render(
      <MemoryRouter>
        <FunctionTable functions={functions} />
      </MemoryRouter>,
    );

    expect(screen.getByRole("link", { name: "Normalize text" }).getAttribute("href")).toBe("/skills/12");
    expect(screen.getByText("User")).toBeTruthy();
    expect(screen.getByText("Integration")).toBeTruthy();
    expect(screen.getByText("Skill is disabled")).toBeTruthy();
    expect(screen.getByText("GitHub connection is not configured")).toBeTruthy();
  });
});
