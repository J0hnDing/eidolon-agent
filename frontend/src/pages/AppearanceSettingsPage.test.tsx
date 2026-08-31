// @vitest-environment jsdom

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { APPEARANCE_STORAGE_KEY } from "../lib/theme";
import UsageSettingsPage from "./UsageSettingsPage";

describe("Appearance settings", () => {
  beforeEach(() => {
    window.localStorage.clear();
    document.documentElement.dataset.theme = "dark";
  });

  it("applies and persists the selected theme without loading backend settings", async () => {
    render(
      <MemoryRouter initialEntries={["/settings/appearance"]}>
        <UsageSettingsPage section="appearance" />
      </MemoryRouter>,
    );

    const lightOption = await screen.findByRole("button", { name: /Light/ });
    fireEvent.click(lightOption);

    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("light"));
    expect(window.localStorage.getItem(APPEARANCE_STORAGE_KEY)).toBe("light");
    expect(lightOption.getAttribute("aria-pressed")).toBe("true");
  });
});
