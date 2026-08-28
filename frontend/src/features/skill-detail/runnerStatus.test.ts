import { describe, expect, it } from "vitest";

import { RunnerStatus } from "../../api/client";
import { runnerBadgeLabel, sandboxStatusLabel } from "./runnerStatus";

function status(overrides: Partial<RunnerStatus> = {}): RunnerStatus {
  return {
    mode: "auto",
    selected_mode: "docker",
    docker_available: true,
    docker_daemon_available: true,
    available: false,
    detail: "Runner image needs a rebuild.",
    image: "personal-agent-skill-runner:latest",
    image_status: "unverified",
    image_ready: false,
    last_build_attempt: "failed",
    last_build_at: "2026-08-27T03:55:26Z",
    image_detail: "The image is not verified.",
    image_build_log: "build failed",
    image_error: "Trusted Docker runner image build failed.",
    ...overrides,
  };
}

describe("runner status presentation", () => {
  it("does not call a healthy daemon with an unverified image active", () => {
    const runner = status();

    expect(runnerBadgeLabel(runner)).toBe("needs rebuild");
    expect(sandboxStatusLabel(runner)).toBe("Docker image rebuild required");
  });

  it("reports the sandbox ready only for a verified current image", () => {
    const runner = status({ available: true, image_ready: true, image_status: "built" });

    expect(runnerBadgeLabel(runner)).toBe("ready");
    expect(sandboxStatusLabel(runner)).toBe("Docker sandbox ready");
  });
});
