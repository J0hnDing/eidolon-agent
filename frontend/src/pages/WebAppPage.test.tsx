// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { WebAppOpenResponse } from "../api/client";
import { WEB_APP_IFRAME_SANDBOX, WebAppFrame, isControlledWebAppEmbedUrl } from "./WebAppPage";

describe("WebAppFrame", () => {
  it("embeds only the controlled origin with the narrow platform sandbox", () => {
    const opened = {
      embed_url: "http://w7-deadbeef-token.web-app.localhost:8000/",
      instance: { id: "instance" },
      session: { id: "session" },
      containment: {},
    } as WebAppOpenResponse;

    render(<WebAppFrame app={opened} title="Example application" />);

    const frame = screen.getByTitle("Example application");
    expect(frame.getAttribute("src")).toBe(opened.embed_url);
    expect(frame.getAttribute("sandbox")).toBe(WEB_APP_IFRAME_SANDBOX);
    expect(frame.getAttribute("sandbox")).not.toContain("allow-top-navigation");
    expect(frame.getAttribute("sandbox")).not.toContain("allow-popups");
    expect(frame.getAttribute("sandbox")).not.toContain("allow-downloads");
    expect(frame.getAttribute("allow")).toBe("");
    expect(frame.getAttribute("referrerpolicy")).toBe("no-referrer");
  });

  it("rejects ordinary external and credential-bearing origins", () => {
    expect(isControlledWebAppEmbedUrl("https://example.com/app")).toBe(false);
    expect(isControlledWebAppEmbedUrl("http://user:secret@demo.web-app.localhost:8000/")).toBe(false);
    expect(isControlledWebAppEmbedUrl("http://demo.web-app.localhost.evil.test/")).toBe(false);
  });
});
