import { describe, expect, it } from "vitest";

import { ProjectConversationState } from "../../api/client";
import { mergeProjectConversationState } from "./projectConversationState";

function state(): ProjectConversationState {
  return {
    generation_request: {
      id: 40,
      user_message: "Build a counter",
      proposed_skill_name: "persistent_counter",
      proposed_display_name: "Persistent Counter",
      plan_json: {},
      requested_permissions_json: {},
      requested_dependencies_json: [],
      requested_network_domains_json: [],
      risk_level: "low",
      status: "generated",
      proposed_skill_id: 2,
      created_at: "2026-07-16T00:00:00Z",
      updated_at: "2026-07-16T00:01:00Z",
      error_message: null,
    },
    permission_request: {
      id: 38,
      skill_id: null,
      generation_request_id: 40,
      schedule_id: null,
      request_scope: "build_time",
      request_type: "generation",
      risk_level: "low",
      requested_permissions_json: {},
      requested_dependencies_json: [],
      requested_network_domains_json: [],
      requested_filesystem_json: {},
      reason_json: {},
      reason: "Build",
      user_explanation: "Build",
      status: "approved",
      created_at: "2026-07-16T00:00:00Z",
      resolved_at: "2026-07-16T00:00:30Z",
      resolved_by: "local_user",
      decision_notes: null,
    },
    proposed_skill: {
      id: 2,
      name: "persistent_counter",
      description: "Counter",
      runtime: "web_app",
      status: "proposed",
      risk_level: "low",
      manifest_path: "skills/proposed/persistent_counter/manifest.json",
      instructions_path: null,
      input_schema_json: null,
      output_schema_json: null,
      installed_path: null,
      active_version_id: null,
      created_at: "2026-07-16T00:00:00Z",
      updated_at: "2026-07-16T00:01:00Z",
      enabled: false,
    },
    agent_run: null,
    runtime_permission_request: null,
    needs_polling: true,
  };
}

describe("mergeProjectConversationState", () => {
  it("recovers a missed inline build approval and generated skill result", () => {
    const messages = mergeProjectConversationState([], state());

    expect(messages.map((message) => message.kind)).toEqual(["build_approval", "generation_result"]);
    expect(messages[0].actionStatus).toBe("approved");
    expect(messages[1].skill?.name).toBe("persistent_counter");
  });

  it("adds and updates runtime approval without duplicating synchronized messages", () => {
    const withRuntime = state();
    withRuntime.runtime_permission_request = {
      ...withRuntime.permission_request!,
      id: 39,
      skill_id: 2,
      generation_request_id: null,
      request_scope: "runtime",
      request_type: "install",
      status: "pending",
      resolved_at: null,
      resolved_by: null,
    };
    const first = mergeProjectConversationState([], withRuntime);
    withRuntime.runtime_permission_request.status = "approved";
    const second = mergeProjectConversationState(first, withRuntime);

    expect(second.filter((message) => message.kind === "runtime_approval")).toHaveLength(1);
    expect(second.find((message) => message.kind === "runtime_approval")?.actionStatus).toBe("approved");
  });
});
