import { ApprovalRequest } from "../../api/client";

export type RuntimePermissionStatus = ApprovalRequest["status"] | "not_analyzed";

export function runtimePermissionRequest(requests: ApprovalRequest[]): ApprovalRequest | null {
  const active = requests.filter((request) => !["expired", "superseded"].includes(request.status));
  const install = active.find((request) => request.request_type === "install") ?? null;
  if (!install) return null;
  const integrations = active.filter((request) => request.request_type === "integration_access");
  const reviews = install.reason_json.integration_requirements;
  if (!Array.isArray(reviews) || integrations.length === 0) return install;
  const integrationRequirements = reviews.map((review) => {
    if (typeof review !== "object" || review === null || Array.isArray(review)) return review;
    const item = review as Record<string, unknown>;
    const matching = integrations.find((request) => integrationMatches(item, request));
    return matching ? { ...item, authorization_state: matching.status } : item;
  });
  return {
    ...install,
    risk_level: highestRisk([install, ...integrations]),
    reason_json: { ...install.reason_json, integration_requirements: integrationRequirements },
  };
}

export function runtimePermissionStatus(request: ApprovalRequest | null): RuntimePermissionStatus {
  if (!request) return "not_analyzed";
  if (request.status !== "approved") return request.status;
  const integrationStates = integrationAuthorizationStates(request);
  if (integrationStates.some((status) => status === "denied")) return "denied";
  if (integrationStates.some((status) => status !== "approved")) return "pending";
  return "approved";
}

export function runtimePermissionsApproved(request: ApprovalRequest | null): boolean {
  return runtimePermissionStatus(request) === "approved";
}

function integrationAuthorizationStates(request: ApprovalRequest): string[] {
  const reviews = request.reason_json.integration_requirements;
  if (!Array.isArray(reviews)) return [];
  return reviews.flatMap((review) => {
    if (typeof review !== "object" || review === null || Array.isArray(review)) return [];
    const state = (review as Record<string, unknown>).authorization_state;
    return typeof state === "string" ? [state] : [];
  });
}

function integrationMatches(review: Record<string, unknown>, request: ApprovalRequest): boolean {
  const reviewFingerprint = review.contract_fingerprint;
  const requestFingerprint = request.reason_json.contract_fingerprint;
  if (typeof reviewFingerprint === "string" && typeof requestFingerprint === "string") {
    return reviewFingerprint === requestFingerprint;
  }
  return review.provider === request.reason_json.provider;
}

function highestRisk(requests: ApprovalRequest[]): ApprovalRequest["risk_level"] {
  const ranks = { low: 0, medium: 1, high: 2, blocked: 3 } as const;
  return requests.reduce(
    (highest, request) => ranks[request.risk_level] > ranks[highest] ? request.risk_level : highest,
    "low" as ApprovalRequest["risk_level"],
  );
}
