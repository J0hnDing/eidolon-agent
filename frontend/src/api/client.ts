const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

export type MemoryCategory =
  | "interests"
  | "goals"
  | "preferences"
  | "routines"
  | "trusted_sources"
  | "blocked_sources"
  | "writing_style"
  | "risk_tolerance";

export type RiskLevel = "low" | "medium" | "high" | "blocked";
export type SkillType = "instruction" | "automation";
export type InterfaceType = "chat" | "tool" | "hidden";
export type SkillStatus = "building" | "proposed" | "installed" | "disabled" | "failed" | "deleted";
export type ChatMode = "chat" | "project";
export type ApprovalStatus = "pending" | "approved" | "denied" | "expired" | "superseded";
export type PermissionRequestScope = "build_time" | "runtime";
export type ScheduleStatus = "pending" | "active" | "paused" | "denied" | "deleted";
export type ScheduleType = "daily" | "weekly" | "interval";
export type AgentRunStatus =
  | "pending"
  | "running"
  | "waiting_for_approval"
  | "paused"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "blocked";
export type AgentRunType = "build_skill" | "repair_skill" | "update_skill";
export type AgentRunStepStatus =
  | "pending"
  | "running"
  | "waiting_for_approval"
  | "succeeded"
  | "failed"
  | "skipped"
  | "cancelled"
  | "blocked";

export interface MemoryFact {
  id: number;
  key: string;
  value: string;
  category: MemoryCategory;
  source_message_id: number | null;
  sensitivity: string;
  expires_at: string | null;
  user_editable: boolean;
  created_at: string;
  updated_at: string;
}

export type MemoryFactInput = Omit<MemoryFact, "id" | "created_at" | "updated_at">;

export interface Skill {
  id: number;
  name: string;
  description: string;
  skill_type: SkillType;
  interface_type: InterfaceType;
  status: SkillStatus;
  risk_level: RiskLevel;
  manifest_path: string;
  instructions_path: string | null;
  input_schema_json: Record<string, unknown> | null;
  output_schema_json: Record<string, unknown> | null;
  tool_ui_schema_json: Record<string, unknown> | null;
  installed_path: string | null;
  active_version_id: number | null;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export type SkillInput = Omit<Skill, "id" | "created_at" | "updated_at" | "active_version_id">;

export type SkillRunStatus = "pending" | "running" | "succeeded" | "failed" | "blocked";

export interface SkillRun {
  id: number;
  skill_id: number;
  status: SkillRunStatus;
  input_json: Record<string, unknown> | null;
  output_json: Record<string, unknown> | null;
  stdout: string | null;
  stderr: string | null;
  exit_code: number | null;
  started_at: string | null;
  ended_at: string | null;
  error_message: string | null;
}

export type SkillVersionStatus = "active" | "draft" | "proposed_update" | "archived" | "discarded";

export interface SkillVersion {
  id: number;
  skill_id: number;
  version: string;
  status: SkillVersionStatus;
  folder_path: string;
  manifest_json: Record<string, unknown>;
  code_snapshot_path: string;
  created_by: "user" | "agent" | "system";
  parent_version_id: number | null;
  permission_fingerprint: string;
  test_status: string;
  validation_status: string;
  change_summary: string | null;
  changelog: string | null;
  created_at: string;
  activated_at: string | null;
}

export interface SkillUpdateResponse {
  agent_run_id: number;
  version: SkillVersion | null;
  permission_request: ApprovalRequest | null;
  status: string;
  message: string;
}

export interface SkillVersionComparison {
  active_version: SkillVersion;
  candidate_version: SkillVersion;
  files: Array<{ path: string; active: string | null; candidate: string | null }>;
}

export interface Tool {
  skill: Skill;
  runtime_permission_status: string;
  runtime_blocked_reason: string | null;
}

export interface ToolRunResponse {
  skill: Skill;
  run: SkillRun;
}

export interface AgentRunStep {
  id: number;
  agent_run_id: number;
  step_name: string;
  task_node_id: string | null;
  milestone_name: string | null;
  status: AgentRunStepStatus;
  input_json: Record<string, unknown> | null;
  output_json: Record<string, unknown> | null;
  logs: string | null;
  started_at: string | null;
  ended_at: string | null;
  error_message: string | null;
  codex_invocations_json: CodexInvocationUsage[];
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  reasoning_output_tokens: number;
  total_tokens: number;
}

export interface CodexInvocationUsage {
  action: string;
  adapter: string;
  model: string | null;
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  reasoning_output_tokens: number;
  total_tokens: number;
}

export interface AgentRun {
  id: number;
  run_type: AgentRunType;
  status: AgentRunStatus;
  skill_id: number | null;
  generation_request_id: number | null;
  user_request: string;
  summary: string | null;
  current_task_id: string | null;
  current_milestone: string | null;
  current_step: string | null;
  failure_count_json: Record<string, number>;
  blueprint_json: Record<string, unknown> | null;
  final_summary_json: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  error_message: string | null;
  total_input_tokens: number;
  total_cached_input_tokens: number;
  total_output_tokens: number;
  total_reasoning_output_tokens: number;
  total_tokens: number;
  pause_reason: string | null;
}

export interface CodexUsageWindow {
  label: string;
  used_percent: number;
  remaining_percent: number;
  window_duration_minutes: number | null;
  resets_at: string | null;
}

export interface CodexAccountUsage {
  available: boolean;
  source: string;
  fetched_at: string;
  error?: string;
  plan_type: string | null;
  limit_id: string;
  rate_limit_reached_type: string | null;
  five_hour: CodexUsageWindow | null;
  weekly: CodexUsageWindow | null;
}

export interface AgentRunDetail extends AgentRun {
  steps: AgentRunStep[];
}

export interface RunnerStatus {
  mode: string;
  selected_mode: string;
  docker_available: boolean;
  available: boolean;
  detail: string;
  image: string | null;
  image_status: string | null;
  image_detail: string | null;
  image_build_log: string | null;
  image_error: string | null;
}

export interface SkillFile {
  path: string;
  content: string;
}

export interface SchedulePayload {
  type: ScheduleType;
  timezone: string;
  input: Record<string, unknown>;
  time?: string | null;
  day?: string | null;
  every?: number | null;
  unit?: "minutes" | "hours" | "days" | null;
}

export interface SkillSchedule {
  id: number;
  skill_id: number;
  skill_name: string | null;
  skill_type: SkillType | null;
  name: string;
  status: ScheduleStatus;
  schedule_type: ScheduleType;
  schedule_json: SchedulePayload;
  input_json: Record<string, unknown>;
  timezone: string;
  next_run_at: string | null;
  last_run_at: string | null;
  last_run_status: string | null;
  created_at: string;
  updated_at: string;
}

export interface ScheduleCreateResponse {
  schedule: SkillSchedule;
  approval_request_id: number | null;
}

export interface ProposedSkillValidation {
  ok: boolean;
  skill_type: SkillType | null;
  manifest_valid: boolean;
  tests_run: boolean;
  tests_passed: boolean | null;
  stdout: string;
  stderr: string;
  error_message: string | null;
  warnings: string[];
}

export interface SkillGenerationRequest {
  id: number;
  user_message: string;
  proposed_skill_name: string;
  proposed_display_name: string;
  proposed_skill_type: SkillType;
  plan_json: Record<string, unknown>;
  requested_permissions_json: Record<string, unknown>;
  requested_dependencies_json: string[];
  requested_network_domains_json: string[];
  risk_level: RiskLevel;
  status: "planned" | "needs_input" | "awaiting_approval" | "approved" | "generating" | "generated" | "failed" | "cancelled";
  proposed_skill_id: number | null;
  created_at: string;
  updated_at: string;
  error_message: string | null;
}

export interface ApprovalRequest {
  id: number;
  skill_id: number | null;
  generation_request_id: number | null;
  request_scope: PermissionRequestScope;
  request_type: string;
  risk_level: RiskLevel;
  requested_permissions_json: Record<string, unknown>;
  requested_dependencies_json: string[];
  requested_network_domains_json: string[];
  requested_filesystem_json: Record<string, unknown>;
  reason_json: Record<string, unknown>;
  reason: string;
  user_explanation: string;
  status: ApprovalStatus;
  created_at: string;
  resolved_at: string | null;
  resolved_by: string | null;
  decision_notes: string | null;
}

export type ChatResponse =
  | {
      type: "direct_answer";
      message: string;
    }
  | {
      type: "skill_generation_plan";
      generation_request: SkillGenerationRequest;
      permission_request: ApprovalRequest;
    }
  | {
      type: "unsafe_or_unsupported";
      message: string;
    }
  | {
      type: "project_not_plausible";
      message: string;
      reason: string;
      optional_projects: string[];
    }
  | {
      type: "project_needs_input";
      message: string;
      question: string;
      generation_request: SkillGenerationRequest;
      agent_run: AgentRun;
    };

export interface SkillGenerationApprovalResponse {
  generation_request: SkillGenerationRequest;
  permission_request: ApprovalRequest | null;
  proposed_skill: Skill | null;
  validation: ProposedSkillValidation | null;
  agent_run: AgentRun | null;
  runtime_permission_request: ApprovalRequest | null;
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        ...options.headers,
      },
    });
  } catch (err) {
    if (err instanceof TypeError && err.message.toLowerCase().includes("fetch")) {
      throw new Error("Could not reach the backend. Confirm the FastAPI server is running and try again.");
    }
    throw err;
  }

  if (!response.ok) {
    const message = await response.text();
    throw new Error(formatApiError(message) || `Request failed with status ${response.status}`);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return response.json() as Promise<T>;
}

function formatApiError(raw: string): string {
  if (!raw) return "";
  try {
    const parsed = JSON.parse(raw) as { detail?: unknown };
    if (typeof parsed.detail === "string") return parsed.detail;
    if (Array.isArray(parsed.detail)) {
      return parsed.detail
        .map((item) => {
          if (item && typeof item === "object" && "msg" in item) {
            return String((item as { msg: unknown }).msg);
          }
          return JSON.stringify(item);
        })
        .join("\n");
    }
  } catch {
    return raw;
  }
  return raw;
}

export const api = {
  sendChatMessage: (message: string, mode: ChatMode, generationRequestId?: number, conversationId?: string) =>
    request<ChatResponse>("/chat", {
      method: "POST",
      body: JSON.stringify({
        message,
        mode,
        generation_request_id: generationRequestId,
        conversation_id: conversationId,
      }),
    }),
  deleteChatConversation: (conversationId: string) =>
    request<void>(`/chat/conversations/${encodeURIComponent(conversationId)}`, {
      method: "DELETE",
    }),
  approveSkillGeneration: (id: number) =>
    request<SkillGenerationApprovalResponse>(`/skill-generation-requests/${id}/approve-generation`, {
      method: "POST",
    }),
  denySkillGeneration: (id: number) =>
    request<SkillGenerationRequest>(`/skill-generation-requests/${id}/deny-generation`, {
      method: "POST",
    }),
  listPermissionRequests: (params: {
    status?: ApprovalStatus;
    request_scope?: PermissionRequestScope;
    skill_id?: number;
    generation_request_id?: number;
  } = {}) => {
    const searchParams = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined) searchParams.set(key, String(value));
    });
    const query = searchParams.toString();
    return request<ApprovalRequest[]>(`/permission-requests${query ? `?${query}` : ""}`);
  },
  approvePermissionRequest: (id: number) =>
    request<ApprovalRequest>(`/permission-requests/${id}/approve`, {
      method: "POST",
      body: JSON.stringify({}),
    }),
  denyPermissionRequest: (id: number) =>
    request<ApprovalRequest>(`/permission-requests/${id}/deny`, {
      method: "POST",
      body: JSON.stringify({}),
    }),
  listMemoryFacts: () => request<MemoryFact[]>("/memory-facts"),
  createMemoryFact: (payload: MemoryFactInput) =>
    request<MemoryFact>("/memory-facts", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  updateMemoryFact: (id: number, payload: Partial<MemoryFactInput>) =>
    request<MemoryFact>(`/memory-facts/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  deleteMemoryFact: (id: number) =>
    request<void>(`/memory-facts/${id}`, {
      method: "DELETE",
    }),
  listSkills: () => request<Skill[]>("/skills"),
  getCodexUsage: () => request<CodexAccountUsage>("/usage/codex"),
  listAgentRuns: () => request<AgentRun[]>("/agent-runs"),
  getAgentRun: (id: number) => request<AgentRunDetail>(`/agent-runs/${id}`),
  listAgentRunSteps: (id: number) => request<AgentRunStep[]>(`/agent-runs/${id}/steps`),
  cancelAgentRun: (id: number) =>
    request<AgentRun>(`/agent-runs/${id}/cancel`, {
      method: "POST",
    }),
  deleteAgentRun: (id: number) =>
    request<void>(`/agent-runs/${id}`, {
      method: "DELETE",
    }),
  resumeAgentRun: (id: number) =>
    request<AgentRun>(`/agent-runs/${id}/resume`, {
      method: "POST",
    }),
  retryCurrentMilestone: (id: number) =>
    request<AgentRun>(`/agent-runs/${id}/retry-current-milestone`, {
      method: "POST",
    }),
  retryCurrentTask: (id: number) =>
    request<AgentRun>(`/agent-runs/${id}/retry-current-task`, {
      method: "POST",
    }),
  retryAgentRunStep: (agentRunId: number, stepId: number) =>
    request<AgentRun>(`/agent-runs/${agentRunId}/retry-step/${stepId}`, {
      method: "POST",
    }),
  listTools: () => request<Tool[]>("/tools"),
  getTool: (id: number) => request<Tool>(`/tools/${id}`),
  runTool: (id: number, input: Record<string, unknown> = {}) =>
    request<ToolRunResponse>(`/tools/${id}/run`, {
      method: "POST",
      body: JSON.stringify({ input }),
    }),
  listProposedSkills: () => request<Skill[]>("/skills/proposed"),
  createSampleProposedSkill: (payload: { name: string; skill_type: SkillType }) =>
    request<Skill>("/skills/proposed/sample", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  createSkill: (payload: SkillInput) =>
    request<Skill>("/skills", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  getRunnerStatus: () => request<RunnerStatus>("/skills/runner-status"),
  getSkill: (id: number) => request<Skill>(`/skills/${id}`),
  listSkillVersions: (id: number) => request<SkillVersion[]>(`/skills/${id}/versions`),
  suggestSkillUpdate: (id: number, suggestion: string) =>
    request<SkillUpdateResponse>(`/skills/${id}/versions/update-suggestion`, {
      method: "POST",
      body: JSON.stringify({ suggestion }),
    }),
  activateSkillVersion: (skillId: number, versionId: number) =>
    request<Skill>(`/skills/${skillId}/versions/${versionId}/activate`, {
      method: "POST",
    }),
  discardSkillVersion: (skillId: number, versionId: number) =>
    request<void>(`/skills/${skillId}/versions/${versionId}/discard`, {
      method: "POST",
    }),
  compareSkillVersion: (skillId: number, versionId: number) =>
    request<SkillVersionComparison>(`/skills/${skillId}/versions/${versionId}/compare`),
  listSchedules: (skillId?: number) =>
    request<SkillSchedule[]>(`/schedules${skillId ? `?skill_id=${skillId}` : ""}`),
  createSchedule: (skillId: number, payload: { name: string; schedule: SchedulePayload }) =>
    request<ScheduleCreateResponse>(`/skills/${skillId}/schedules`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  createManifestSchedule: (skillId: number) =>
    request<ScheduleCreateResponse>(`/skills/${skillId}/schedules/from-manifest`, {
      method: "POST",
    }),
  approveSchedule: (id: number) =>
    request<SkillSchedule>(`/schedules/${id}/approve`, {
      method: "POST",
    }),
  denySchedule: (id: number) =>
    request<SkillSchedule>(`/schedules/${id}/deny`, {
      method: "POST",
    }),
  pauseSchedule: (id: number) =>
    request<SkillSchedule>(`/schedules/${id}/pause`, {
      method: "POST",
    }),
  resumeSchedule: (id: number) =>
    request<SkillSchedule>(`/schedules/${id}/resume`, {
      method: "POST",
    }),
  deleteSchedule: (id: number) =>
    request<void>(`/schedules/${id}`, {
      method: "DELETE",
    }),
  runScheduleNow: (id: number) =>
    request<SkillRun>(`/schedules/${id}/run-now`, {
      method: "POST",
    }),
  listSkillFiles: (id: number) => request<SkillFile[]>(`/skills/${id}/files`),
  validateSkill: (id: number) =>
    request<ProposedSkillValidation>(`/skills/${id}/validate`, {
      method: "POST",
    }),
  analyzeRuntimePermissions: (id: number) =>
    request<ApprovalRequest>(`/skills/${id}/runtime-permissions/analyze`, {
      method: "POST",
    }),
  approveRuntimePermissions: (id: number) =>
    request<ApprovalRequest>(`/skills/${id}/runtime-permissions/approve`, {
      method: "POST",
    }),
  denyRuntimePermissions: (id: number) =>
    request<ApprovalRequest>(`/skills/${id}/runtime-permissions/deny`, {
      method: "POST",
    }),
  installSkill: (id: number) =>
    request<Skill>(`/skills/${id}/install`, {
      method: "POST",
    }),
  rejectSkill: (id: number) =>
    request<void>(`/skills/${id}/reject`, {
      method: "POST",
    }),
  repairSkill: (id: number) =>
    request<AgentRun>(`/skills/${id}/repair`, {
      method: "POST",
    }),
  runSkill: (id: number, input: Record<string, unknown> = {}) =>
    request<SkillRun>(`/skills/${id}/run`, {
      method: "POST",
      body: JSON.stringify({ input }),
    }),
  listSkillRuns: (id: number) => request<SkillRun[]>(`/skills/${id}/runs`),
  updateSkill: (id: number, payload: Partial<SkillInput>) =>
    request<Skill>(`/skills/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  deleteSkill: (id: number) =>
    request<void>(`/skills/${id}`, {
      method: "DELETE",
    }),
};
