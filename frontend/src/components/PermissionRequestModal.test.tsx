// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ApprovalRequest } from "../api/client";
import PermissionRequestModal from "./PermissionRequestModal";

describe("PermissionRequestModal integration review", () => {
  it("shows every integration inside the single runtime approval", () => {
    const request: ApprovalRequest = {
      id: 1,
      skill_id: 2,
      generation_request_id: null,
      schedule_id: null,
      request_scope: "runtime",
      request_type: "install",
      risk_level: "low",
      requested_permissions_json: {},
      requested_dependencies_json: [],
      requested_network_domains_json: [],
      requested_filesystem_json: {},
      reason_json: {
        integration_requirements: [
          {
            provider: "github",
            operations: ["github.repository.get", "github.repository.file.read"],
            read_only: true,
            resource_scope: { repositories: ["octo/demo"] },
            connection_available: false,
            authorization_state: "pending",
          },
          {
            provider: "notion",
            operations: ["notion.todos.list", "notion.todos.create"],
            read_only: false,
            resource_scope: { repositories: [] },
            connection_available: true,
            authorization_state: "pending",
          },
        ],
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

    expect(screen.getByText("Integration Access")).toBeTruthy();
    expect(screen.getByText("GitHub")).toBeTruthy();
    expect(screen.getByText("Notion")).toBeTruthy();
    expect(screen.getByText("github.repository.get")).toBeTruthy();
    expect(screen.getByText("github.repository.file.read")).toBeTruthy();
    expect(screen.getByText("notion.todos.create")).toBeTruthy();
    expect(screen.getByText("Scope: octo/demo")).toBeTruthy();
    expect(screen.getByText("Scope: provider-local only")).toBeTruthy();
    expect(screen.getByText(/connection unavailable/)).toBeTruthy();
    expect(screen.getByText(/bounded write access/)).toBeTruthy();
  });
});
