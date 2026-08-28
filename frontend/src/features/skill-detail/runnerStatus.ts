import { RunnerStatus } from "../../api/client";

export function runnerBadgeLabel(status: RunnerStatus | null): string {
  if (!status) return "unknown";
  if (status.selected_mode === "local") return status.available ? "available" : "blocked";
  if (!status.docker_daemon_available) return "blocked";
  return status.image_ready ? "ready" : "needs rebuild";
}

export function sandboxStatusLabel(status: RunnerStatus | null): string {
  if (!status) return "Runner status unavailable";
  if (status.selected_mode === "local") return "Local dev runner";
  if (!status.docker_daemon_available) return "Docker sandbox unavailable";
  return status.image_ready && status.available
    ? "Docker sandbox ready"
    : "Docker image rebuild required";
}
