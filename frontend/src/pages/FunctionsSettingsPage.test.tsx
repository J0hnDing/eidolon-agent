// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import FunctionsSettingsPage from "./FunctionsSettingsPage";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("FunctionsSettingsPage", () => {
  it("keeps the catalog within Settings navigation", async () => {
    vi.spyOn(api, "listFunctionCatalog").mockResolvedValue([]);

    render(
      <MemoryRouter initialEntries={["/settings/functions"]}>
        <FunctionsSettingsPage />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { level: 1, name: "Functions" })).toBeTruthy();
    const functionsLink = screen.getByRole("link", { name: "Functions" });
    expect(functionsLink.getAttribute("href")).toBe("/settings/functions");
    expect(functionsLink.getAttribute("aria-current")).toBe("page");
    expect(screen.getByRole("link", { name: "Appearance" }).getAttribute("href")).toBe("/settings/appearance");
  });
});
