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
export type SkillType = "instruction" | "automation" | "hybrid";
export type InterfaceType = "chat" | "tool" | "hidden";
export type SkillStatus = "proposed" | "installed" | "disabled" | "failed" | "deleted";
export type ChatMode = "chat" | "project";
export type ApprovalStatus = "pending" | "approved" | "denied" | "expired" | "superseded";
export type PermissionRequestScope = "build_time" | "runtime";
export type ScheduleStatus = "pending" | "active" | "paused" | "denied" | "deleted";
export type ScheduleType = "daily" | "weekly" | "interval";

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
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export type SkillInput = Omit<Skill, "id" | "created_at" | "updated_at">;

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

export interface Tool {
  skill: Skill;
  runtime_permission_status: string;
  runtime_blocked_reason: string | null;
}

export interface ToolRunResponse {
  skill: Skill;
  run: SkillRun;
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
  status: "planned" | "awaiting_approval" | "approved" | "generating" | "generated" | "failed" | "cancelled";
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
    };

export interface SkillGenerationApprovalResponse {
  generation_request: SkillGenerationRequest;
  permission_request: ApprovalRequest | null;
  proposed_skill: Skill | null;
  validation: ProposedSkillValidation | null;
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...options.headers,
    },
  });

  if (!response.ok) {
    const message = await response.text();
    throw new Error(message || `Request failed with status ${response.status}`);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return response.json() as Promise<T>;
}

export const api = {
  sendChatMessage: (message: string, mode: ChatMode) =>
    request<ChatResponse>("/chat", {
      method: "POST",
      body: JSON.stringify({ message, mode }),
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
