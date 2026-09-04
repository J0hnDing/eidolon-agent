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
export type SkillRuntime = "function" | "web_app" | "service";
export type SkillStatus = "building" | "proposed" | "installed" | "failed" | "deleted";
export type FunctionCategory = "backend_core" | "user" | "integration";
export type FunctionAvailability = "available" | "disabled" | "unavailable" | "error";
export type ConversationMode = "project" | "act";
export type ApprovalStatus = "pending" | "approved" | "denied" | "expired" | "superseded";
export type PermissionRequestScope = "build_time" | "runtime";
export type ScheduleStatus = "active" | "paused";
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
  runtime: SkillRuntime;
  status: SkillStatus;
  risk_level: RiskLevel;
  manifest_path: string;
  instructions_path: string | null;
  input_schema_json: Record<string, unknown> | null;
  output_schema_json: Record<string, unknown> | null;
  installed_path: string | null;
  active_version_id: number | null;
  enabled: boolean;
  is_running: boolean;
  created_at: string;
  updated_at: string;
}

export interface FunctionCatalogEntry {
  id: string;
  category: FunctionCategory;
  title: string;
  description: string;
  risk_level: RiskLevel;
  input_schema: Record<string, unknown> | null;
  output_schema: Record<string, unknown> | null;
  availability: FunctionAvailability;
  availability_reasons: string[];
  invocation: Record<string, unknown>;
  call_name: string | null;
  provider: string | null;
  skill_id: number | null;
  active_version: string | null;
  is_running: boolean;
}

export type SkillUpdateInput = Pick<Skill, "enabled">;

export type SkillRunStatus = "pending" | "running" | "succeeded" | "partial" | "failed" | "blocked";

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
  codex_invocations_json: CodexInvocationUsage[];
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  reasoning_output_tokens: number;
  total_tokens: number;
  schedule_occurrence_key?: string | null;
  scheduled_for_at?: string | null;
  schedule_trigger?: "automatic" | "startup_catch_up" | null;
}

export type WebAppInstanceStatus = "starting" | "ready" | "healthy" | "unhealthy" | "stopped" | "failed";

export interface WebAppInstance {
  id: string;
  skill_id: number;
  version_id: number;
  status: WebAppInstanceStatus;
  runner_mode: string;
  container_id: string | null;
  relay_container_id: string | null;
  process_id: number | null;
  error_message: string | null;
  logs: string | null;
  created_at: string;
  started_at: string | null;
  ready_at: string | null;
  last_accessed_at: string | null;
  stopped_at: string | null;
  updated_at: string;
}

export interface WebAppSession {
  id: string;
  instance_id: string;
  skill_id: number;
  status: "active" | "closed" | "expired";
  gateway_host: string;
  created_at: string;
  last_accessed_at: string;
  expires_at: string;
  closed_at: string | null;
}

export interface WebAppContainmentPolicy {
  iframe_sandbox: string;
  content_security_policy: string;
  permissions_policy: string;
  browser_network: string;
  origin_isolation: string;
  websocket_support: string;
  runner_isolation: string;
  server_network_enforcement: string;
}

export interface WebAppOpenResponse {
  instance: WebAppInstance;
  session: WebAppSession;
  embed_url: string;
  containment: WebAppContainmentPolicy;
}

export interface WebAppAuditRecord {
  id: number;
  instance_id: string;
  session_id: string | null;
  operation: string;
  status: string;
  request_json: Record<string, unknown>;
  response_json: Record<string, unknown>;
  error_message: string | null;
  started_at: string;
  ended_at: string | null;
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

export interface AgentRunStep {
  id: number;
  agent_run_id: number;
  step_name: string;
  action: string | null;
  task_node_id: string | null;
  approval_request_id: number | null;
  status: AgentRunStepStatus;
  input_json: Record<string, unknown> | null;
  output_json: Record<string, unknown> | null;
  agent_input_text: string | null;
  agent_output_text: string | null;
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
  status?: "succeeded" | "failed";
  model: string | null;
  requested_model?: string | null;
  effective_model?: string | null;
  requested_reasoning_effort?: string | null;
  effective_reasoning_effort?: string | null;
  route_source?: string | null;
  role?: string | null;
  difficulty?: string | null;
  cli_path?: string | null;
  cli_version?: string | null;
  cli_source?: string | null;
  exit_code?: number | null;
  error_type?: string | null;
  error_message?: string | null;
  stderr_tail?: string | null;
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
  current_step: string | null;
  failure_count_json: Record<string, number>;
  build_workflow: string | null;
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

export interface CodexCliCandidate {
  path: string;
  source: "explicit_override" | "codex_desktop" | "path" | string;
  version: string | null;
  error: string | null;
}

export interface CodexCliStatus {
  available: boolean;
  compatible: boolean;
  requested_command: string | null;
  explicit_override: boolean;
  resolved_path: string | null;
  source: "explicit_override" | "codex_desktop" | "path" | null | string;
  version: string | null;
  minimum_version: string | null;
  error: string | null;
  candidates: CodexCliCandidate[];
}

export interface CodexInvocationChoice {
  model: string | null;
  reasoning_effort: string | null;
}

export type ProjectBuildWorkflowOverride = "single_codex" | "task_dag";

export interface CodexRoutingSettingsPayload {
  project_build_workflow_override: ProjectBuildWorkflowOverride | null;
  act: CodexInvocationChoice;
  product_manager: {
    default: CodexInvocationChoice;
    blueprint_and_permissions: CodexInvocationChoice;
    task_dag: CodexInvocationChoice;
    repair: CodexInvocationChoice;
    update: CodexInvocationChoice;
  };
  builder: {
    default: CodexInvocationChoice;
    single_codex: CodexInvocationChoice;
    easy: CodexInvocationChoice;
    medium: CodexInvocationChoice;
    hard: CodexInvocationChoice;
    repair: CodexInvocationChoice;
    update: CodexInvocationChoice;
  };
  tester: {
    default: CodexInvocationChoice;
    task: CodexInvocationChoice;
    final_e2e: CodexInvocationChoice;
    update: CodexInvocationChoice;
  };
}

export interface CodexRoutingSettings extends CodexRoutingSettingsPayload {
  updated_at: string | null;
}

export interface ActTurn {
  id: number;
  session_id: number;
  codex_turn_id: string | null;
  user_message: string;
  assistant_message: string | null;
  activity_json: Array<{ kind: string; label: string }>;
  status: string;
  error_message: string | null;
  cancel_requested_at: string | null;
  delivery_status: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}

export interface ActSession {
  id: number;
  title: string;
  origin: string;
  status: string;
  created_at: string;
  updated_at: string;
  turns: ActTurn[];
}

export type ActSessionSummary = Omit<ActSession, "turns">;

export interface PermissionPolicy {
  source: string;
  default_allowed: Record<string, unknown>;
  requires_approval: Record<string, unknown>;
  blocked: string[];
  web_app: {
    supported: string[];
    blocked: string[];
  };
}

export interface CodexModelOption {
  id: string;
  model: string;
  display_name: string;
  description: string;
  is_default: boolean;
  default_reasoning_effort: string;
  supported_reasoning_efforts: string[];
}

export interface CodexModelCatalog {
  available: boolean;
  fetched_at: string;
  error: string | null;
  models: CodexModelOption[];
}

export interface CodexMcpStatus {
  enabled: boolean;
  registered: boolean;
  config_matches: boolean;
  available_tool_count: number;
  excluded_ids: string[];
  config_path: string;
  restart_required: boolean;
  error_type: string | null;
  error: string | null;
}

export interface GitHubConnectionStatus {
  provider: "github";
  connected: boolean;
  status: "connected" | "disconnected" | "unavailable" | "invalid";
  account_login: string | null;
  account_id: string | null;
  last_validated_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  error_type: string | null;
}

export interface NotionConnectionStatus {
  provider: "notion";
  connected: boolean;
  status: "connected" | "disconnected" | "unavailable" | "invalid";
  bot_name: string | null;
  bot_id: string | null;
  workspace_name: string | null;
  data_source_id: string | null;
  report_data_source_id: string | null;
  last_validated_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  error_type: string | null;
}

export interface QuercusCourse {
  course_id: string;
  name: string;
  course_code: string | null;
  term_name: string | null;
  enrollment_state: string | null;
  selected: boolean;
  retained: boolean;
  local_path: string | null;
  last_sync_started_at: string | null;
  last_sync_completed_at: string | null;
  last_sync_status: string | null;
  last_error_type: string | null;
  skipped_file_count: number;
  last_processing_started_at: string | null;
  last_processing_completed_at: string | null;
  last_processing_status: string | null;
  last_processing_error_type: string | null;
  processed_file_count: number;
  failed_processing_count: number;
}

export type QuercusProcessingMethod = "none" | "marker_surya_llamacpp";

export interface QuercusProcessingStatus {
  method: QuercusProcessingMethod;
  llama_cpp_directory: string | null;
  llama_cpp_available: boolean;
  inference_url: string | null;
  marker_available: boolean;
  inference_available: boolean;
  status: "disabled" | "idle" | "pending" | "running" | "succeeded" | "partial" | "failed";
  processed_file_count: number;
  failed_file_count: number;
}

export interface QuercusProcessingReprocessResult extends QuercusProcessingStatus {
  queued_file_count: number;
}

export interface QuercusConnectionStatus {
  provider: "quercus";
  connected: boolean;
  status: "connected" | "disconnected" | "unavailable" | "invalid";
  account_name: string | null;
  account_id: string | null;
  last_validated_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  error_type: string | null;
  courses: QuercusCourse[];
}

export interface GoogleCalendarConnectionStatus {
  provider: "google_calendar";
  connected: boolean;
  status: "connected" | "disconnected" | "unavailable" | "invalid";
  account_email: string | null;
  last_validated_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  error_type: string | null;
  oauth_redirect_uri: string;
}

export interface GoogleOAuthClientStatus {
  provider: "google";
  configured: boolean;
  status: "configured" | "not_configured" | "unavailable" | "conflict";
  calendar_redirect_uri: string;
  gmail_redirect_uri: string;
  created_at: string | null;
  updated_at: string | null;
  error_type: string | null;
}

export interface GmailConnectionStatus {
  provider: "gmail";
  connected: boolean;
  status: "connected" | "disconnected" | "unavailable" | "invalid";
  account_email: string | null;
  last_validated_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  error_type: string | null;
  oauth_redirect_uri: string;
}

export interface TelegramConnectionStatus {
  provider: "telegram";
  connected: boolean;
  status: "connected" | "disconnected" | "pairing" | "unavailable" | "invalid" | "webhook_conflict";
  bot_username: string | null;
  paired_chat_id: string | null;
  paired_user_id: string | null;
  pairing_expires_at: string | null;
  last_validated_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  error_type: string | null;
}

export interface TelegramPairingResponse {
  connection: TelegramConnectionStatus;
  pairing_code: string;
  expires_at: string;
}

export interface AtlasIntegrationStatus {
  provider: "atlas";
  directory: string;
  running: boolean;
  process_ownership: "owned" | "external" | "none";
  initialized: boolean | null;
  locked: boolean | null;
  passphrase_configured: boolean;
  startup_error?: string | null;
  error_type?: string | null;
}

export interface AgentRunDetail extends AgentRun {
  steps: AgentRunStep[];
}

export interface RunnerStatus {
  mode: string;
  selected_mode: string;
  docker_available: boolean;
  docker_daemon_available: boolean;
  available: boolean;
  detail: string;
  image: string | null;
  image_status: string | null;
  image_ready: boolean;
  last_build_attempt: string | null;
  last_build_at: string | null;
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
  schedule_kind: "service" | "platform";
  service_id: string | null;
  read_only: boolean;
  skill_id: number | null;
  skill_name: string | null;
  skill_enabled: boolean | null;
  is_running: boolean;
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

export interface ProposedSkillValidation {
  ok: boolean;
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
  schedule_id: number | null;
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

export interface InvocationApproval {
  id: number;
  target_kind: string;
  target_id: string;
  target_skill_id: number | null;
  target_version_id: number | null;
  target_contract_fingerprint: string;
  target_description: string;
  provider: string | null;
  provider_account_id: string | null;
  caller_type: string;
  source: string;
  caller_skill_id: number | null;
  caller_version_id: number | null;
  caller_run_id: number | null;
  web_app_instance_id: string | null;
  initiating_action: string | null;
  input_json: Record<string, unknown>;
  input_hash: string;
  reason_to_call: string;
  presentation_json: InvocationApprovalPresentation;
  dispatch_metadata_json: Record<string, unknown>;
  decision_status: "pending" | "approved" | "denied";
  execution_status: "not_started" | "executing" | "succeeded" | "failed" | "stale" | "outcome_unknown";
  decided_via: string | null;
  decided_by: string | null;
  telegram_delivery_status: string;
  telegram_message_ids_json: number[];
  result_json: Record<string, unknown> | null;
  error_type: string | null;
  error_message: string | null;
  created_at: string;
  delivered_at: string | null;
  decided_at: string | null;
  execution_started_at: string | null;
  execution_completed_at: string | null;
  updated_at: string;
}

export interface InvocationApprovalPresentation {
  version: number;
  preset: string;
  action: string;
  caller: string;
  fields: Array<{
    label: string;
    value: string;
    multiline: boolean;
  }>;
  reason: string;
}

export interface PendingApprovalReceipt {
  status: "pending_approval";
  approval_id: number;
}

export type ChatResponse =
  | {
      type: "skill_generation_plan";
      generation_request: SkillGenerationRequest;
      permission_request: ApprovalRequest;
    }
  | {
      type: "project_not_plausible";
      message: string;
      reason: string;
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

export interface ProjectConversationState {
  generation_request: SkillGenerationRequest;
  permission_request: ApprovalRequest | null;
  proposed_skill: Skill | null;
  agent_run: AgentRun | null;
  runtime_permission_request: ApprovalRequest | null;
  needs_polling: boolean;
}

const inFlightGetRequests = new Map<string, Promise<unknown>>();

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const method = (options.method ?? "GET").toUpperCase();
  const requestKey = method === "GET" ? path : null;
  const existing = requestKey ? inFlightGetRequests.get(requestKey) : undefined;
  if (existing) return existing as Promise<T>;

  const pending = performRequest<T>(path, options);
  if (requestKey) inFlightGetRequests.set(requestKey, pending);
  try {
    return await pending;
  } finally {
    if (requestKey && inFlightGetRequests.get(requestKey) === pending) {
      inFlightGetRequests.delete(requestKey);
    }
  }
}

async function performRequest<T>(path: string, options: RequestInit): Promise<T> {
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
    if (
      parsed.detail
      && typeof parsed.detail === "object"
      && "message" in parsed.detail
      && typeof (parsed.detail as { message?: unknown }).message === "string"
    ) {
      return (parsed.detail as { message: string }).message;
    }
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
  sendProjectMessage: (message: string, generationRequestId?: number, conversationId?: string) =>
    request<ChatResponse>("/chat", {
      method: "POST",
      body: JSON.stringify({
        message,
        generation_request_id: generationRequestId,
        conversation_id: conversationId,
      }),
    }),
  deleteChatConversation: (conversationId: string) =>
    request<void>(`/chat/conversations/${encodeURIComponent(conversationId)}`, {
      method: "DELETE",
    }),
  listActSessions: () => request<ActSessionSummary[]>("/act/sessions"),
  createActSession: () => request<ActSession>("/act/sessions", { method: "POST", body: JSON.stringify({ origin: "web" }) }),
  getActSession: (sessionId: number) => request<ActSession>(`/act/sessions/${sessionId}`),
  runActTurn: (sessionId: number, message: string) => request<ActTurn>(`/act/sessions/${sessionId}/turns`, { method: "POST", body: JSON.stringify({ message }) }),
  cancelActTurn: (sessionId: number, turnId: number) => request<ActTurn>(`/act/sessions/${sessionId}/turns/${turnId}/cancel`, { method: "POST" }),
  archiveActSession: (sessionId: number) => request<void>(`/act/sessions/${sessionId}`, { method: "DELETE" }),
  approveSkillGeneration: (id: number) =>
    request<SkillGenerationApprovalResponse>(`/skill-generation-requests/${id}/approve-generation`, {
      method: "POST",
    }),
  denySkillGeneration: (id: number) =>
    request<SkillGenerationRequest>(`/skill-generation-requests/${id}/deny-generation`, {
      method: "POST",
    }),
  getProjectConversationState: (conversationId: string) =>
    request<ProjectConversationState | null>(
      `/skill-generation-requests/conversation/${encodeURIComponent(conversationId)}`,
    ),
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
  listInvocationApprovals: () =>
    request<InvocationApproval[]>("/invocation-approvals"),
  getInvocationApproval: (id: number) =>
    request<InvocationApproval>(`/invocation-approvals/${id}`),
  approveInvocationApproval: (id: number) =>
    request<InvocationApproval>(`/invocation-approvals/${id}/approve`, {
      method: "POST",
      body: JSON.stringify({}),
    }),
  denyInvocationApproval: (id: number) =>
    request<InvocationApproval>(`/invocation-approvals/${id}/deny`, {
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
  openActRoot: () => request<void>("/act/workspace/open-root", { method: "POST" }),
  listSkills: () => request<Skill[]>("/skills"),
  listFunctionCatalog: () => request<FunctionCatalogEntry[]>("/functions/catalog"),
  openWebApp: (skillId: number) =>
    request<WebAppOpenResponse>(`/web-apps/${skillId}/sessions`, { method: "POST" }),
  listWebAppInstances: (skillId: number) => request<WebAppInstance[]>(`/web-apps/${skillId}/instances`),
  listWebAppAudit: (skillId: number) => request<WebAppAuditRecord[]>(`/web-apps/${skillId}/audit`),
  stopWebApp: (skillId: number) =>
    request<WebAppInstance[]>(`/web-apps/${skillId}/stop`, { method: "POST" }),
  getCodexUsage: () => request<CodexAccountUsage>("/usage/codex"),
  getCodexCliStatus: (refresh = false) => request<CodexCliStatus>(`/usage/codex/cli?refresh=${refresh}`),
  getCodexRoutingSettings: () => request<CodexRoutingSettings>("/settings/codex-routing"),
  getPermissionPolicy: () => request<PermissionPolicy>("/settings/permission-policy"),
  updateCodexRoutingSettings: (payload: CodexRoutingSettingsPayload) =>
    request<CodexRoutingSettings>("/settings/codex-routing", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  getCodexModels: (refresh = false) =>
    request<CodexModelCatalog>(`/settings/codex-models?refresh=${refresh}`),
  getCodexMcpStatus: () => request<CodexMcpStatus>("/settings/codex-mcp"),
  updateCodexMcp: (action: "install" | "repair") =>
    request<CodexMcpStatus>("/settings/codex-mcp", {
      method: "PUT",
      body: JSON.stringify({ action }),
    }),
  removeCodexMcp: () =>
    request<CodexMcpStatus>("/settings/codex-mcp", { method: "DELETE" }),
  getGitHubConnection: () => request<GitHubConnectionStatus>("/settings/integrations/github"),
  putGitHubConnection: (token: string) =>
    request<GitHubConnectionStatus>("/settings/integrations/github", {
      method: "PUT",
      body: JSON.stringify({ token }),
    }),
  removeGitHubConnection: () =>
    request<void>("/settings/integrations/github", { method: "DELETE" }),
  getNotionConnection: () => request<NotionConnectionStatus>("/settings/integrations/notion"),
  putNotionConnection: (token: string) =>
    request<NotionConnectionStatus>("/settings/integrations/notion", {
      method: "PUT",
      body: JSON.stringify({ token }),
    }),
  putNotionDataSources: (dataSourceId: string, reportDataSourceId: string) =>
    request<NotionConnectionStatus>("/settings/integrations/notion/data-sources", {
      method: "PUT",
      body: JSON.stringify({
        data_source_id: dataSourceId,
        report_data_source_id: reportDataSourceId,
      }),
    }),
  removeNotionDataSources: () =>
    request<NotionConnectionStatus>("/settings/integrations/notion/data-sources", {
      method: "DELETE",
    }),
  removeNotionConnection: () =>
    request<void>("/settings/integrations/notion", { method: "DELETE" }),
  getQuercusConnection: () => request<QuercusConnectionStatus>("/settings/integrations/quercus"),
  putQuercusConnection: (token: string) =>
    request<QuercusConnectionStatus>("/settings/integrations/quercus", {
      method: "PUT",
      body: JSON.stringify({ token }),
    }),
  removeQuercusConnection: () =>
    request<QuercusConnectionStatus>("/settings/integrations/quercus", { method: "DELETE" }),
  getQuercusCourses: () => request<QuercusCourse[]>("/settings/integrations/quercus/courses"),
  putQuercusCourses: (courseIds: string[]) =>
    request<QuercusCourse[]>("/settings/integrations/quercus/courses", {
      method: "PUT",
      body: JSON.stringify({ course_ids: courseIds }),
    }),
  deleteQuercusCourse: (courseId: string) =>
    request<void>(`/settings/integrations/quercus/courses/${encodeURIComponent(courseId)}`, {
      method: "DELETE",
    }),
  getQuercusProcessing: () =>
    request<QuercusProcessingStatus>("/settings/integrations/quercus/processing"),
  putQuercusProcessing: (method: QuercusProcessingMethod, llamaCppDirectory: string | null) =>
    request<QuercusProcessingStatus>("/settings/integrations/quercus/processing", {
      method: "PUT",
      body: JSON.stringify({ method, llama_cpp_directory: llamaCppDirectory }),
    }),
  reprocessFailedQuercusFiles: () =>
    request<QuercusProcessingReprocessResult>(
      "/settings/integrations/quercus/processing/reprocess-failed",
      { method: "POST" },
    ),
  getGoogleCalendarConnection: () =>
    request<GoogleCalendarConnectionStatus>("/settings/integrations/google-calendar"),
  getGoogleOAuthClient: () =>
    request<GoogleOAuthClientStatus>("/settings/integrations/google"),
  putGoogleOAuthClient: (clientId: string, clientSecret: string) =>
    request<GoogleOAuthClientStatus>("/settings/integrations/google/oauth-client", {
      method: "PUT",
      body: JSON.stringify({ client_id: clientId, client_secret: clientSecret }),
    }),
  removeGoogleOAuthClient: () =>
    request<GoogleOAuthClientStatus>("/settings/integrations/google/oauth-client", { method: "DELETE" }),
  startGoogleCalendarOAuth: () =>
    request<{ authorization_url: string }>("/settings/integrations/google-calendar/oauth/start", {
      method: "POST",
    }),
  removeGoogleCalendarConnection: () =>
    request<void>("/settings/integrations/google-calendar", { method: "DELETE" }),
  getGmailConnection: () =>
    request<GmailConnectionStatus>("/settings/integrations/gmail"),
  startGmailOAuth: () =>
    request<{ authorization_url: string }>("/settings/integrations/gmail/oauth/start", {
      method: "POST",
    }),
  removeGmailConnection: () =>
    request<void>("/settings/integrations/gmail", { method: "DELETE" }),
  getTelegramConnection: () =>
    request<TelegramConnectionStatus>("/settings/integrations/telegram"),
  getTelegramAgentConnection: () => request<TelegramConnectionStatus>("/settings/integrations/telegram-agent"),
  startTelegramPairing: (token: string) =>
    request<TelegramPairingResponse>("/settings/integrations/telegram/pairing/start", {
      method: "POST",
      body: JSON.stringify({ token }),
    }),
  startTelegramAgentPairing: (token: string) => request<TelegramPairingResponse>("/settings/integrations/telegram-agent/pairing/start", { method: "POST", body: JSON.stringify({ token }) }),
  refreshTelegramPairing: () =>
    request<TelegramConnectionStatus>("/settings/integrations/telegram/pairing/refresh", {
      method: "POST",
    }),
  refreshTelegramAgentPairing: () =>
    request<TelegramConnectionStatus>("/settings/integrations/telegram-agent/pairing/refresh", {
      method: "POST",
    }),
  removeTelegramConnection: () =>
    request<void>("/settings/integrations/telegram", { method: "DELETE" }),
  removeTelegramAgentConnection: () => request<void>("/settings/integrations/telegram-agent", { method: "DELETE" }),
  getAtlasStatus: () => request<AtlasIntegrationStatus>("/settings/integrations/atlas"),
  updateAtlasDirectory: (directory: string) =>
    request<AtlasIntegrationStatus>("/settings/integrations/atlas/directory", {
      method: "PUT",
      body: JSON.stringify({ directory }),
    }),
  putAtlasPassphrase: (passphrase: string) =>
    request<AtlasIntegrationStatus>("/settings/integrations/atlas/passphrase", {
      method: "PUT",
      body: JSON.stringify({ passphrase }),
    }),
  removeAtlasPassphrase: () =>
    request<void>("/settings/integrations/atlas/passphrase", { method: "DELETE" }),
  unlockAtlas: () =>
    request<AtlasIntegrationStatus>("/settings/integrations/atlas/unlock", { method: "POST" }),
  restartAtlas: () =>
    request<AtlasIntegrationStatus>("/settings/integrations/atlas/restart", { method: "POST" }),
  listAgentRuns: () => request<AgentRun[]>("/agent-runs"),
  getAgentRun: (id: number) => request<AgentRunDetail>(`/agent-runs/${id}`),
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
  retryCurrentTask: (id: number) =>
    request<AgentRun>(`/agent-runs/${id}/retry-current-task`, {
      method: "POST",
    }),
  retryAgentRunStep: (agentRunId: number, stepId: number) =>
    request<AgentRun>(`/agent-runs/${agentRunId}/retry-step/${stepId}`, {
      method: "POST",
    }),
  listProposedSkills: () => request<Skill[]>("/skills/proposed"),
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
  updateSchedule: (id: number, payload: { name: string; schedule: SchedulePayload }) =>
    request<SkillSchedule>(`/schedules/${id}`, {
      method: "PUT",
      body: JSON.stringify(payload),
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
    request<SkillRun | PendingApprovalReceipt>(`/skills/${id}/run`, {
      method: "POST",
      body: JSON.stringify({ input }),
    }),
  listSkillRuns: (id: number) => request<SkillRun[]>(`/skills/${id}/runs`),
  updateSkill: (id: number, payload: Partial<SkillUpdateInput>) =>
    request<Skill>(`/skills/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  deleteSkill: (id: number) =>
    request<void>(`/skills/${id}`, {
      method: "DELETE",
    }),
};
