// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import MemoryPage from "./MemoryPage";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("MemoryPage", () => {
  it("opens the backend-owned Act root from the page header", async () => {
    vi.spyOn(api, "listMemoryFacts").mockResolvedValue([]);
    const open = vi.spyOn(api, "openActRoot").mockResolvedValue(undefined);

    render(<MemoryPage />);
    fireEvent.click(screen.getByRole("button", { name: "Open agent folder" }));

    await waitFor(() => expect(open).toHaveBeenCalledOnce());
  });
});
