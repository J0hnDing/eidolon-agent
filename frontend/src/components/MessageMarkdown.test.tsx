// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { MessageMarkdown } from "./MessageMarkdown";

afterEach(cleanup);

it("renders readable Markdown with links, lists, code, and tables", () => {
  const { container } = render(<MessageMarkdown content={"**Important**\n\n- First task\n- Second task\n\n[Course](https://example.com/course)\n\n```python\nprint('hello')\n```\n\n| Item | State |\n| --- | --- |\n| Review | Ready |"} />);
  expect(screen.getByText("Important").tagName).toBe("STRONG");
  expect(screen.getAllByRole("listitem")).toHaveLength(2);
  expect(screen.getByRole("link", { name: "Course" }).getAttribute("href")).toBe("https://example.com/course");
  expect(container.querySelector("pre code")?.textContent).toContain("print('hello')");
  expect(screen.getByRole("table")).toBeTruthy();
});

it("does not execute HTML, expose executable links, or automatically load remote images", () => {
  const { container } = render(<MessageMarkdown content={'<script>alert("unsafe")</script>\n\n[Unsafe](javascript:alert%281%29)\n\n![Photo](https://example.com/private.png)'} />);
  expect(container.querySelector("script")).toBeNull();
  expect(container.querySelector("img")).toBeNull();
  expect(screen.queryByRole("link", { name: "Unsafe" })).toBeNull();
  expect(screen.getByRole("link", { name: "Photo" }).getAttribute("rel")).toContain("noreferrer");
});
