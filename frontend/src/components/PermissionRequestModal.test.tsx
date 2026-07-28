// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ApprovalRequest } from "../api/client";
import PermissionRequestModal from "./PermissionRequestModal";

describe("PermissionRequestModal integration review", () => {
  it("shows the sanitized GitHub authorization contract", () => {
    const request: ApprovalRequest = {
      id: 1,
      skill_id: 2,
      generation_request_id: null,
      schedule_id: null,
      request_scope: "runtime",
      request_type: "integration_access",
      risk_level: "low",
      requested_permissions_json: {},
      requested_dependencies_json: [],
      requested_network_domains_json: [],
      requested_filesystem_json: {},
      reason_json: {
        provider: "github",
        operations: ["github.repository.get", "github.repository.file.read"],
        reason: "Read approved repository content.",
        read_only: true,
        resource_scope: { repositories: ["octo/demo"] },
        connection_available: false,
      },
      reason: "Approve read-only GitHub integration access.",
      user_explanation: "Approve read-only GitHub integration access.",
      status: "pending",
      created_at: "2026-01-01T00:00:00Z",
      resolved_at: null,
      resolved_by: null,
      decision_notes: null,
    };

    render(
      <PermissionRequestModal
        request={request}
        title="Review integration"
        isWorking={false}
        onApprove={vi.fn()}
        onDeny={vi.fn()}
      />,
    );

    expect(screen.getByText("GitHub integration authorization")).toBeTruthy();
    expect(screen.getByText("github.repository.get")).toBeTruthy();
    expect(screen.getByText("github.repository.file.read")).toBeTruthy();
    expect(screen.getByText("Read approved repository content.")).toBeTruthy();
    expect(screen.getByText("Repositories: octo/demo")).toBeTruthy();
    expect(screen.getByText(/connection unavailable/)).toBeTruthy();
  });
});
