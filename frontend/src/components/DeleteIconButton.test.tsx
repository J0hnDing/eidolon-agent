// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DeleteIconButton } from "./DeleteIconButton";

describe("DeleteIconButton", () => {
  it("renders a labelled square trash-icon control without visible button text", () => {
    render(<DeleteIconButton label="Delete example" />);

    const button = screen.getByRole("button", { name: "Delete example" });
    expect(button.classList.contains("square-icon-button")).toBe(true);
    expect(button.getAttribute("title")).toBe("Delete example");
    expect(button.textContent).toBe("");
    expect(button.querySelector('svg[aria-hidden="true"]')).toBeTruthy();
  });
});
