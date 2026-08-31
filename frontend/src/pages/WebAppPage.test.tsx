// @vitest-environment jsdom

import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Skill, WebAppOpenResponse, api } from "../api/client";
import WebAppPage, { WEB_APP_IFRAME_SANDBOX, WebAppFrame, isControlledWebAppEmbedUrl } from "./WebAppPage";

afterEach(() => {
  vi.restoreAllMocks();
});

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
    expect(frame.getAttribute("sandbox")).toContain("allow-modals");
    expect(frame.getAttribute("sandbox")).not.toContain("allow-top-navigation");
    expect(frame.getAttribute("sandbox")).not.toContain("allow-popups");
    expect(frame.getAttribute("sandbox")).not.toContain("allow-downloads");
    expect(frame.getAttribute("allow")).toBe("");
    expect(frame.getAttribute("referrerpolicy")).toBe("no-referrer");
  });

  it("opens only one session when Strict Mode replays the mount effect", async () => {
    const skill = {
      id: 7,
      name: "notes",
      description: "Notes",
      runtime: "web_app",
    } as Skill;
    const opened = {
      embed_url: "http://w7-deadbeef-token.web-app.localhost:8000/",
      instance: { id: "instance", status: "stopped" },
      session: { id: "session" },
      containment: {},
    } as WebAppOpenResponse;
    vi.spyOn(api, "getSkill").mockResolvedValue(skill);
    const openSpy = vi.spyOn(api, "openWebApp").mockResolvedValue(opened);
    vi.spyOn(api, "listWebAppInstances").mockResolvedValue([]);
    vi.spyOn(api, "listWebAppAudit").mockResolvedValue([]);

    render(
      <React.StrictMode>
        <MemoryRouter initialEntries={["/apps/7"]}>
          <Routes>
            <Route path="/apps/:skillId" element={<WebAppPage />} />
          </Routes>
        </MemoryRouter>
      </React.StrictMode>,
    );

    await waitFor(() => expect(openSpy).toHaveBeenCalledTimes(1));
    expect(await screen.findByTitle("Notes application")).toBeTruthy();
  });

  it("rejects ordinary external and credential-bearing origins", () => {
    expect(isControlledWebAppEmbedUrl("https://example.com/app")).toBe(false);
    expect(isControlledWebAppEmbedUrl("http://user:secret@demo.web-app.localhost:8000/")).toBe(false);
    expect(isControlledWebAppEmbedUrl("http://demo.web-app.localhost.evil.test/")).toBe(false);
  });
});
