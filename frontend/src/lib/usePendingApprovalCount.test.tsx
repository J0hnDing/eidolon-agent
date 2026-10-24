// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import { usePendingApprovalCount } from "./usePendingApprovalCount";

function CountReader() {
  const count = usePendingApprovalCount();
  return <p>Pending: {count ?? "loading"}</p>;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("usePendingApprovalCount", () => {
  it("aggregates only pending items and refreshes after approval changes", async () => {
    const permissions = vi.spyOn(api, "listPermissionRequests").mockResolvedValue([
      { status: "pending" },
    ] as Awaited<ReturnType<typeof api.listPermissionRequests>>);
    const invocations = vi.spyOn(api, "listInvocationApprovals").mockResolvedValue([
      { decision_status: "pending" },
      { decision_status: "approved" },
      { decision_status: "pending" },
    ] as Awaited<ReturnType<typeof api.listInvocationApprovals>>);
    const proposals = vi.spyOn(api, "listAssistantProposals").mockResolvedValue([
      { status: "pending" },
      { status: "denied" },
    ] as Awaited<ReturnType<typeof api.listAssistantProposals>>);

    render(<CountReader />);

    expect(await screen.findByText("Pending: 4")).toBeTruthy();
    expect(permissions).toHaveBeenCalledWith({ status: "pending" });

    window.dispatchEvent(new Event("eidolon:approvals-changed"));
    await waitFor(() => expect(permissions).toHaveBeenCalledTimes(2));
    expect(invocations).toHaveBeenCalledTimes(2);
    expect(proposals).toHaveBeenCalledTimes(2);
  });
});
