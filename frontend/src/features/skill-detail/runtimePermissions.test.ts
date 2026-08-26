import { describe, expect, it } from "vitest";

import { ApprovalRequest } from "../../api/client";
import {
  runtimePermissionRequest,
  runtimePermissionStatus,
  runtimePermissionsApproved,
} from "./runtimePermissions";

function request(id: number, requestType: string, status: ApprovalRequest["status"]): ApprovalRequest {
  return {
    id,
    skill_id: 7,
    generation_request_id: null,
    schedule_id: null,
    request_scope: "runtime",
    request_type: requestType,
    risk_level: "low",
    requested_permissions_json: {},
    requested_dependencies_json: [],
    requested_network_domains_json: [],
    requested_filesystem_json: {},
    reason_json: {},
    reason: "Review this request.",
    user_explanation: "Review this request.",
    status,
    created_at: "2026-08-26T00:00:00Z",
    resolved_at: null,
    resolved_by: null,
    decision_notes: null,
  };
}

describe("runtime permission bundle", () => {
  it("uses the install request as the single visible approval", () => {
    const integration = request(57, "integration_access", "pending");
    integration.reason_json = { provider: "notion", contract_fingerprint: "notion-v1" };
    const install = request(56, "install", "approved");
    install.reason_json.integration_requirements = [{
      provider: "notion",
      contract_fingerprint: "notion-v1",
      operations: ["notion.todos.list"],
      authorization_state: "approved",
    }];
    const combined = runtimePermissionRequest([integration, install]);

    expect(combined?.request_type).toBe("install");
    expect(runtimePermissionStatus(combined)).toBe("pending");
  });

  it("shows one pending bundle when base permissions are approved but an integration is pending", () => {
    const install = request(56, "install", "approved");
    install.reason_json.integration_requirements = [{
      provider: "notion",
      operations: ["notion.todos.list"],
      authorization_state: "pending",
    }];

    expect(runtimePermissionStatus(install)).toBe("pending");
    expect(runtimePermissionsApproved(install)).toBe(false);
  });

  it("is approved only when the base and every bundled integration are approved", () => {
    const install = request(56, "install", "approved");
    install.reason_json.integration_requirements = [
      { provider: "notion", operations: ["notion.todos.list"], authorization_state: "approved" },
      { provider: "github", operations: ["github.repository.read"], authorization_state: "approved" },
    ];

    expect(runtimePermissionsApproved(install)).toBe(true);
  });
});
