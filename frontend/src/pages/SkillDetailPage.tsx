import { FormEvent, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import PermissionRequestModal from "../components/PermissionRequestModal";
import {
  RunHistory,
  RunOutput,
  SkillFilesDisclosure,
  UpdateChatMessage,
  UpdateSuggestionChat,
  ValidationResult,
  VersionComparisonPanel,
  VersionList,
  isNonEmptyObject,
  parseRunInput,
  replaceUpdateMessage,
  updateResponseText,
  updateUpdateMessage,
} from "../features/skill-detail/SkillDetailPanels";
import {
  runtimePermissionRequest,
  runtimePermissionStatus,
  runtimePermissionsApproved,
} from "../features/skill-detail/runtimePermissions";
import { runnerBadgeLabel, sandboxStatusLabel } from "../features/skill-detail/runnerStatus";
import { formatDisplayName } from "../lib/displayName";
import { usePolling } from "../lib/usePolling";
import {
  AgentRun,
  ApprovalRequest,
  CodexModelCatalog,
  PendingApprovalReceipt,
  ProposedSkillValidation,
  RunnerStatus,
  Skill,
  SkillFile,
  SkillRun,
  SkillSchedule,
  SkillModelSetting,
  SkillUpdateResponse,
  SkillVersion,
  SkillVersionComparison,
  api,
} from "../api/client";

const LIVE_AGENT_RUN_STATUSES = new Set(["pending", "running", "waiting_for_approval"]);
const LIVE_SKILL_STATUSES = new Set(["building"]);

export default function SkillDetailPage() {
  const { skillId } = useParams();
  const navigate = useNavigate();
  const [skill, setSkill] = useState<Skill | null>(null);
  const [runs, setRuns] = useState<SkillRun[]>([]);
  const [pendingApproval, setPendingApproval] = useState<PendingApprovalReceipt | null>(null);
  const [files, setFiles] = useState<SkillFile[]>([]);
  const [validation, setValidation] = useState<ProposedSkillValidation | null>(null);
  const [runtimePermission, setRuntimePermission] = useState<ApprovalRequest | null>(null);
  const [runnerStatus, setRunnerStatus] = useState<RunnerStatus | null>(null);
  const [skillModel, setSkillModel] = useState<SkillModelSetting | null>(null);
  const [modelCatalog, setModelCatalog] = useState<CodexModelCatalog | null>(null);
  const [selectedModel, setSelectedModel] = useState("");
  const [savedModel, setSavedModel] = useState("");
  const [selectedReasoningEffort, setSelectedReasoningEffort] = useState("");
  const [savedReasoningEffort, setSavedReasoningEffort] = useState("");
  const [schedules, setSchedules] = useState<SkillSchedule[]>([]);
  const [agentRuns, setAgentRuns] = useState<AgentRun[]>([]);
  const [versions, setVersions] = useState<SkillVersion[]>([]);
  const [versionComparison, setVersionComparison] = useState<SkillVersionComparison | null>(null);
  const [updateSuggestion, setUpdateSuggestion] = useState("");
  const [updateResponse, setUpdateResponse] = useState<SkillUpdateResponse | null>(null);
  const [updateMessages, setUpdateMessages] = useState<UpdateChatMessage[]>([]);
  const [showRuntimeModal, setShowRuntimeModal] = useState(false);
  const [runInput, setRunInput] = useState('{\n  "hello": "world"\n}');
  const [isLoading, setIsLoading] = useState(true);
  const [isRunning, setIsRunning] = useState(false);
  const [isWorking, setIsWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const runOutput = runs[0] ?? null;

  useEffect(() => {
    loadSkillDetail();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [skillId]);

  const hasLiveAgentRun = agentRuns.some((run) => LIVE_AGENT_RUN_STATUSES.has(run.status));
  const shouldPollSkillDetail = Boolean(
    skill && (
      LIVE_SKILL_STATUSES.has(skill.status)
      || hasLiveAgentRun
      || runtimePermissionStatus(runtimePermission) === "pending"
    ),
  );

  usePolling(() => loadSkillDetail({ showLoading: false }), shouldPollSkillDetail, 2000);

  async function loadSkillDetail(options: { showLoading?: boolean } = {}) {
    if (!skillId) return;
    if (options.showLoading !== false) setIsLoading(true);
    setError(null);
    try {
      const id = Number(skillId);
      const [loadedSkill, loadedSkillModel, loadedModelCatalog, loadedRuns, loadedFiles, permissionRequests, loadedRunnerStatus, loadedSchedules, loadedAgentRuns, loadedVersions] = await Promise.all([
        api.getSkill(id),
        api.getSkillModel(id),
        api.getCodexModels().catch(() => null),
        api.listSkillRuns(id),
        api.listSkillFiles(id),
        api.listPermissionRequests({ skill_id: id, request_scope: "runtime" }),
        api.getRunnerStatus(),
        api.listSchedules(id),
        api.listAgentRuns(),
        api.listSkillVersions(id).catch(() => []),
      ]);
      setSkill(loadedSkill);
      setSkillModel(loadedSkillModel);
      setSelectedModel(loadedSkillModel.model ?? "");
      setSavedModel(loadedSkillModel.model ?? "");
      setSelectedReasoningEffort(loadedSkillModel.reasoning_effort ?? "");
      setSavedReasoningEffort(loadedSkillModel.reasoning_effort ?? "");
      setModelCatalog(loadedModelCatalog);
      setRuns(loadedRuns);
      setFiles(loadedFiles);
      applyRuntimePermissionRequests(permissionRequests);
      setRunnerStatus(loadedRunnerStatus);
      setSchedules(loadedSchedules);
      setAgentRuns(loadedAgentRuns.filter((run) => run.skill_id === id));
      setVersions(loadedVersions);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load skill");
    } finally {
      if (options.showLoading !== false) setIsLoading(false);
    }
  }

  function applyRuntimePermissionRequests(requests: ApprovalRequest[]) {
    setRuntimePermission(runtimePermissionRequest(requests));
  }

  async function handleSaveModel(event: FormEvent) {
    event.preventDefault();
    if (!skill || !skillModel) return;
    setIsWorking(true);
    setError(null);
    try {
      const saved = await api.updateSkillModel(
        skill.id,
        selectedModel || null,
        selectedReasoningEffort || null,
      );
      setSkillModel(saved);
      setSelectedModel(saved.model ?? "");
      setSavedModel(saved.model ?? "");
      setSelectedReasoningEffort(saved.reasoning_effort ?? "");
      setSavedReasoningEffort(saved.reasoning_effort ?? "");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save skill model");
    } finally {
      setIsWorking(false);
    }
  }

  async function handleRun() {
    if (!skill) return;
    if (!runtimePermissionsApproved(runtimePermission)) {
      await handleReviewRuntimePermissions(true);
      return;
    }
    setIsRunning(true);
    setError(null);
    try {
      const parsedInput = parseRunInput(runInput);
      if (parsedInput === null || Array.isArray(parsedInput) || typeof parsedInput !== "object") {
        throw new Error("Run input must be a JSON object");
      }
      const run = await api.runSkill(skill.id, parsedInput as Record<string, unknown>);
      if ("status" in run && run.status === "pending_approval") {
        setPendingApproval(run);
        return;
      }
      setPendingApproval(null);
      setRuns((current) => [run, ...current.filter((item) => item.id !== run.id)]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not run skill");
    } finally {
      setIsRunning(false);
    }
  }

  async function handleValidate() {
    if (!skill) return;
    setIsWorking(true);
    setError(null);
    try {
      setValidation(await api.validateSkill(skill.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not validate skill");
    } finally {
      setIsWorking(false);
    }
  }

  async function handleInstall() {
    if (!skill) return;
    if (!runtimePermissionsApproved(runtimePermission)) {
      await handleReviewRuntimePermissions(true);
      return;
    }
    setIsWorking(true);
    setError(null);
    try {
      const installed = await api.installSkill(skill.id);
      setSkill(installed);
      setValidation(null);
      setFiles(await api.listSkillFiles(installed.id));
    } catch (err) {
      try {
        const recovered = await api.getSkill(skill.id);
        if (recovered.status === "installed") {
          setSkill(recovered);
          setValidation(null);
          setFiles(await api.listSkillFiles(recovered.id));
          return;
        }
      } catch {
        // Preserve the original mutation error when state cannot be reconciled.
      }
      setError(err instanceof Error ? err.message : "Could not install skill");
    } finally {
      setIsWorking(false);
    }
  }

  async function handleReject() {
    if (!skill) return;
    setIsWorking(true);
    setError(null);
    try {
      await api.rejectSkill(skill.id);
      navigate("/skills");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not reject skill");
    } finally {
      setIsWorking(false);
    }
  }

  async function handleDelete() {
    if (!skill) return;
    setIsWorking(true);
    setError(null);
    try {
      await api.deleteSkill(skill.id);
      navigate("/skills");
    } catch (err) {
      try {
        const remaining = await api.listSkills();
        if (!remaining.some((item) => item.id === skill.id)) {
          navigate("/skills");
          return;
        }
      } catch {
        // Preserve the original mutation error when state cannot be reconciled.
      }
      setError(err instanceof Error ? err.message : "Could not delete skill");
    } finally {
      setIsWorking(false);
    }
  }

  async function handleRepair() {
    if (!skill) return;
    setIsWorking(true);
    setError(null);
    try {
      const agentRun = await api.repairSkill(skill.id);
      setAgentRuns((current) => [agentRun, ...current.filter((run) => run.id !== agentRun.id)]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start repair agent run");
    } finally {
      setIsWorking(false);
    }
  }

  async function refreshVersions() {
    if (!skill) return;
    setVersions(await api.listSkillVersions(skill.id));
  }

  async function handleSuggestUpdate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!skill) return;
    const suggestion = updateSuggestion.trim();
    if (!suggestion) return;
    const requestMessageId = Date.now();
    setUpdateMessages((current) => [
      ...current,
      {
        id: requestMessageId,
        role: "user",
        content: `Suggest an update for ${formatDisplayName(skill.name)}: ${suggestion}`,
      },
      {
        id: requestMessageId + 1,
        role: "assistant",
        kind: "thinking",
        content: `ProductManager is reviewing the update suggestion for ${formatDisplayName(skill.name)}...`,
      },
    ]);
    setIsWorking(true);
    setError(null);
    setUpdateResponse(null);
    try {
      const response = await api.suggestSkillUpdate(skill.id, suggestion);
      setUpdateResponse(response);
      setUpdateSuggestion("");
      setUpdateMessages((current) =>
        replaceUpdateMessage(current, requestMessageId + 1, [
        {
          id: Date.now(),
          role: "assistant",
          kind: response.permission_request ? "build_approval" : "text",
          content: updateResponseText(formatDisplayName(skill.name), response),
          permissionRequest: response.permission_request ?? undefined,
          actionStatus: response.permission_request?.status === "pending" ? "pending" : undefined,
          agentRunId: response.agent_run_id,
        },
        ]),
      );
      await refreshVersions();
      const loadedAgentRuns = await api.listAgentRuns();
      setAgentRuns(loadedAgentRuns.filter((run) => run.skill_id === skill.id));
    } catch (err) {
      const message = err instanceof Error ? err.message : "Could not start update workflow";
      setUpdateMessages((current) =>
        replaceUpdateMessage(current, requestMessageId + 1, [
        {
          id: Date.now(),
          role: "assistant",
          content: `ProductManager could not start the update workflow for ${formatDisplayName(skill.name)}.\n\n${message}`,
        },
        ]),
      );
      setError(message);
    } finally {
      setIsWorking(false);
    }
  }

  async function handleApproveUpdatePermission(message: UpdateChatMessage) {
    if (!message.permissionRequest || !message.agentRunId) return;
    setUpdateMessages((current) =>
      updateUpdateMessage(current, message.id, (item) => ({ ...item, actionStatus: "working" })),
    );
    setIsWorking(true);
    setError(null);
    try {
      const request = await api.approvePermissionRequest(message.permissionRequest.id);
      await api.resumeAgentRun(message.agentRunId);
      setUpdateMessages((current) =>
        updateUpdateMessage(current, message.id, (item) => ({
          ...item,
          permissionRequest: request,
          actionStatus: "approved",
          content: `${item.content}\n\nApproved. Builder and Tester are now creating the draft version.`,
        })),
      );
      await loadSkillDetail({ showLoading: false });
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "Could not approve update";
      setUpdateMessages((current) =>
        updateUpdateMessage(current, message.id, (item) => ({ ...item, actionStatus: "failed" })),
      );
      setError(errorMessage);
    } finally {
      setIsWorking(false);
    }
  }

  async function handleDenyUpdatePermission(message: UpdateChatMessage) {
    if (!message.permissionRequest) return;
    setUpdateMessages((current) =>
      updateUpdateMessage(current, message.id, (item) => ({ ...item, actionStatus: "working" })),
    );
    setIsWorking(true);
    setError(null);
    try {
      const request = await api.denyPermissionRequest(message.permissionRequest.id);
      setUpdateMessages((current) =>
        updateUpdateMessage(current, message.id, (item) => ({
          ...item,
          permissionRequest: request,
          actionStatus: "denied",
          content: `${item.content}\n\nDeclined. No draft version was created.`,
        })),
      );
      await loadSkillDetail({ showLoading: false });
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "Could not decline update";
      setUpdateMessages((current) =>
        updateUpdateMessage(current, message.id, (item) => ({ ...item, actionStatus: "failed" })),
      );
      setError(errorMessage);
    } finally {
      setIsWorking(false);
    }
  }

  async function handleActivateVersion(versionId: number) {
    if (!skill) return;
    setIsWorking(true);
    setError(null);
    try {
      const updated = await api.activateSkillVersion(skill.id, versionId);
      setSkill(updated);
      setFiles(await api.listSkillFiles(updated.id));
      await refreshVersions();
      applyRuntimePermissionRequests(
        await api.listPermissionRequests({ skill_id: updated.id, request_scope: "runtime" }),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not activate version");
    } finally {
      setIsWorking(false);
    }
  }

  async function handleDiscardVersion(versionId: number) {
    if (!skill) return;
    setIsWorking(true);
    setError(null);
    try {
      await api.discardSkillVersion(skill.id, versionId);
      setVersionComparison(null);
      await refreshVersions();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not discard version");
    } finally {
      setIsWorking(false);
    }
  }

  async function handleCompareVersion(versionId: number) {
    if (!skill) return;
    setIsWorking(true);
    setError(null);
    try {
      setVersionComparison(await api.compareSkillVersion(skill.id, versionId));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not compare version");
    } finally {
      setIsWorking(false);
    }
  }

  async function handleReviewRuntimePermissions(openModal = true) {
    if (!skill) return;
    setIsWorking(true);
    setError(null);
    try {
      const request = await api.analyzeRuntimePermissions(skill.id);
      setRuntimePermission(request);
      setShowRuntimeModal(openModal);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not analyze runtime permissions");
    } finally {
      setIsWorking(false);
    }
  }

  async function handleApproveRuntimePermissions() {
    if (!skill || !runtimePermission) return;
    setIsWorking(true);
    setError(null);
    try {
      setRuntimePermission(await api.approveRuntimePermissions(skill.id));
      setShowRuntimeModal(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not approve runtime permissions");
    } finally {
      setIsWorking(false);
    }
  }

  async function handleDenyRuntimePermissions() {
    if (!skill || !runtimePermission) return;
    setIsWorking(true);
    setError(null);
    try {
      setRuntimePermission(await api.denyRuntimePermissions(skill.id));
      setShowRuntimeModal(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not deny runtime permissions");
    } finally {
      setIsWorking(false);
    }
  }

  async function handleToggleEnabled() {
    if (!skill) return;
    setIsWorking(true);
    setError(null);
    try {
      if (skill.runtime === "service" && !skill.enabled) {
        const permissionRequest = await api.analyzeRuntimePermissions(skill.id);
        setRuntimePermission(permissionRequest);
        if (!runtimePermissionsApproved(permissionRequest)) {
          setShowRuntimeModal(true);
          return;
        }
      }
      setSkill(await api.updateSkill(skill.id, { enabled: !skill.enabled }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not update skill");
    } finally {
      setIsWorking(false);
    }
  }

  if (isLoading) {
    return <p className="muted">Loading skill...</p>;
  }

  if (error || !skill) {
    return (
      <section className="page stack">
        <Link className="skill-back-button" to="/skills" aria-label="Back to skills" title="Back to skills">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="m14 6-6 6 6 6M8 12h10" />
          </svg>
        </Link>
        <p className="error-text">{error ?? "Skill not found"}</p>
      </section>
    );
  }

  const isInstalled = skill.status === "installed";
  const isFunction = skill.runtime === "function";
  const isWebApp = skill.runtime === "web_app";
  const isService = skill.runtime === "service";
  const serviceSchedule = schedules[0] ?? null;
  const runtimeStatus = runtimePermissionStatus(runtimePermission);
  const runtimeApproved = runtimePermissionsApproved(runtimePermission);
  const canRun = isFunction && isInstalled && skill.enabled && runtimeApproved;
  const isProposed = skill.status === "proposed";
  const displayName = formatDisplayName(skill.name);
  const modelOptions = modelCatalog?.models ?? [];
  const selectedModelOption = modelOptions.find(
    (option) => option.model === selectedModel || option.id === selectedModel,
  ) ?? (selectedModel === "" ? modelOptions.find((option) => option.is_default) ?? modelOptions[0] : undefined);
  const effortOptions = selectedModelOption?.supported_reasoning_efforts ?? [];
  const savedModelIsListed = Boolean(
    savedModel && modelOptions.some((option) => option.model === savedModel || option.id === savedModel),
  );
  const selectedEffortIsListed = Boolean(
    selectedReasoningEffort && effortOptions.includes(selectedReasoningEffort),
  );

  return (
    <section className="page stack skill-detail-page">
      <header className="skill-detail-header">
        <div className="skill-detail-title">
          <Link className="skill-back-button" to="/skills" aria-label="Back to skills" title="Back to skills">
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="m14 6-6 6 6 6M8 12h10" />
            </svg>
          </Link>
          <h1>{displayName}</h1>
        </div>
        <button
          type="button"
          className="danger"
          onClick={isProposed ? handleReject : handleDelete}
          disabled={isWorking}
        >
          Delete Skill
        </button>
      </header>

      <section className="detail-panel">
        <p>{skill.description}</p>
        <dl className="detail-grid">
          <div>
            <dt>Runtime</dt>
            <dd>{skill.runtime}</dd>
          </div>
          <div>
            <dt>Status</dt>
            <dd>{skill.status}</dd>
          </div>
          <div>
            <dt>Risk Level</dt>
            <dd>{skill.risk_level}</dd>
          </div>
          <div>
            <dt>Enabled</dt>
            <dd>{skill.enabled ? "enabled" : "disabled"}</dd>
          </div>
          <div>
            <dt>Manifest Path</dt>
            <dd>{skill.manifest_path}</dd>
          </div>
          <div>
            <dt>Instructions Path</dt>
            <dd>{skill.instructions_path ?? "none"}</dd>
          </div>
          <div>
            <dt>Installed Path</dt>
            <dd>{skill.installed_path ?? "not installed"}</dd>
          </div>
          <div>
            <dt>Active Version</dt>
            <dd>{versions.find((version) => version.id === skill.active_version_id)?.version ?? skill.active_version_id ?? "none"}</dd>
          </div>
          <div>
            <dt>Input Schema</dt>
            <dd>{skill.input_schema_json ? "declared" : "none"}</dd>
          </div>
          <div>
            <dt>Output Schema</dt>
            <dd>{skill.output_schema_json ? "declared" : "none"}</dd>
          </div>
        </dl>
        {isProposed ? (
          <div className="button-row skill-primary-actions">
            <button type="button" onClick={handleValidate} disabled={isWorking}>
              Validate/Test
            </button>
            <button type="button" onClick={handleInstall} disabled={isWorking}>
              Install
            </button>
            <button type="button" className="secondary" onClick={handleRepair} disabled={isWorking}>
              Ask Agents to Repair
            </button>
          </div>
        ) : (
          <div className="button-row skill-primary-actions">
            {isWebApp && isInstalled && skill.enabled && runtimeApproved && (
              <Link className="button-link" to={`/apps/${skill.id}`}>Open Application</Link>
            )}
            {isInstalled && skill.enabled && !runtimeApproved && (
              <button type="button" onClick={() => handleReviewRuntimePermissions(true)} disabled={isWorking}>
                {isService ? "Review Before Activation" : "Review Before Run"}
              </button>
            )}
            {isInstalled && !skill.enabled && !isService && (
              <button type="button" disabled>
                Run Disabled
              </button>
            )}
            <button type="button" onClick={handleToggleEnabled} disabled={isWorking}>
              {skill.enabled ? "Disable" : "Enable"}
            </button>
          </div>
        )}
      </section>

      <section className="detail-panel stack">
        <header className="page-header">
          <div>
            <h2>Codex Model</h2>
            <p className="muted">
              Choose the model and reasoning effort used by this skill&apos;s backend Codex calls. Leaving these at their defaults uses the existing global/request defaults.
            </p>
          </div>
          {(selectedModel !== savedModel || selectedReasoningEffort !== savedReasoningEffort) && <span className="badge">unsaved</span>}
        </header>
        <form onSubmit={handleSaveModel}>
          <label>
            Model
            <select
              aria-label="Codex model"
              value={selectedModel}
              onChange={(event) => {
                setSelectedModel(event.target.value);
                setSelectedReasoningEffort("");
              }}
              disabled={isWorking || !skillModel}
            >
              <option value="">Use global default</option>
              {savedModel && !savedModelIsListed && <option value={savedModel}>{savedModel} (unavailable)</option>}
              {modelOptions.map((option) => (
                <option key={option.id || option.model} value={option.model}>
                  {option.display_name || option.model}
                </option>
              ))}
            </select>
          </label>
          <label>
            Reasoning effort
            <select
              aria-label="Codex reasoning effort"
              value={selectedReasoningEffort}
              onChange={(event) => setSelectedReasoningEffort(event.target.value)}
              disabled={isWorking || !skillModel || !selectedModel}
            >
              <option value="">Use model/global default</option>
              {selectedReasoningEffort && !selectedEffortIsListed && (
                <option value={selectedReasoningEffort}>{selectedReasoningEffort} (unavailable)</option>
              )}
              {effortOptions.map((effort) => <option key={effort} value={effort}>{effort}</option>)}
            </select>
          </label>
          <div className="button-row">
            <button
              type="submit"
              disabled={
                isWorking
                || !skillModel
                || (selectedModel === savedModel && selectedReasoningEffort === savedReasoningEffort)
              }
            >
              {isWorking ? "Saving..." : "Save model"}
            </button>
            {modelCatalog && !modelCatalog.available && (
              <span className="muted">Model choices are unavailable until Codex is connected.</span>
            )}
          </div>
        </form>
      </section>

      <section className="detail-panel">
        <header className="page-header">
          <div>
            <h2>Versions</h2>
            <p className="muted">
              Updates are built in copied version folders. Activating a version switches the active pointer only after validation and permission checks.
            </p>
          </div>
          <span className="badge">{versions.length}/3 versions</span>
        </header>
        {versions.length >= 3 && (
          <p className="error-text">Maximum 3 versions reached. Discard a draft or proposed update before creating another.</p>
        )}
        <VersionList
          versions={versions}
          activeVersionId={skill.active_version_id}
          isWorking={isWorking}
          onCompare={handleCompareVersion}
          onActivate={handleActivateVersion}
          onDiscard={handleDiscardVersion}
        />
        {versionComparison && <VersionComparisonPanel comparison={versionComparison} />}
        {skill.status === "installed" && (
          <form className="skill-improvement-form" onSubmit={handleSuggestUpdate}>
            <div>
              <h3>Suggest an Improvement</h3>
              <p className="muted">Describe a focused change, or ask the agents to repair the current skill.</p>
            </div>
            <textarea
              value={updateSuggestion}
              onChange={(event) => setUpdateSuggestion(event.target.value)}
              placeholder="Describe a specific improvement to this skill"
              rows={4}
              required
            />
            <div className="button-row">
              <button type="submit" disabled={isWorking || versions.length >= 3}>
                Start Update Workflow
              </button>
              <button type="button" className="secondary" onClick={handleRepair} disabled={isWorking}>
                Ask Agents to Repair
              </button>
            </div>
          </form>
        )}
        <UpdateSuggestionChat
          messages={updateMessages}
          isWorking={isWorking}
          latestResponse={updateResponse}
          onApprove={handleApproveUpdatePermission}
          onDeny={handleDenyUpdatePermission}
        />
      </section>

      <section className="detail-panel">
        <header className="page-header">
          <div>
            <h2>Runtime Permissions</h2>
            <p className="muted">
              {runtimePermission
                ? runtimePermission.user_explanation || runtimePermission.reason
                : "Runtime permissions have not been analyzed yet."}
            </p>
          </div>
          <span className={`badge status-${runtimeStatus}`}>
            {runtimeStatus.replace("_", " ")}
          </span>
        </header>
        {isNonEmptyObject(runtimePermission?.reason_json?.permission_expansion) && (
          <p className="error-text">Permission expansion detected. Review the generated manifest before approving.</p>
        )}
        {!runtimeApproved && (
          <div className="button-row">
            <button type="button" className="secondary" onClick={() => handleReviewRuntimePermissions(true)} disabled={isWorking}>
              Review Runtime Permissions
            </button>
          </div>
        )}
      </section>

      <section className="detail-panel">
        <header className="page-header">
          <div>
            <h2>Runner / Sandbox</h2>
            <p className="muted">
              {runnerStatus?.detail ?? "Runner status is not available."}
            </p>
          </div>
          <span className={`badge ${runnerStatus?.available ? "run-succeeded" : "run-blocked"}`}>
            {runnerBadgeLabel(runnerStatus)}
          </span>
        </header>
        <dl className="detail-grid">
          <div>
            <dt>Configured Mode</dt>
            <dd>{runnerStatus?.mode ?? "unknown"}</dd>
          </div>
          <div>
            <dt>Selected Runner</dt>
            <dd>{runnerStatus?.selected_mode ?? "unknown"}</dd>
          </div>
          <div>
            <dt>Docker Daemon</dt>
            <dd>{runnerStatus?.docker_daemon_available ? "available" : "unavailable"}</dd>
          </div>
          <div>
            <dt>Image</dt>
            <dd>{runnerStatus?.image ?? "not applicable"}</dd>
          </div>
          <div>
            <dt>Image Status</dt>
            <dd>{runnerStatus?.image_status ?? "unknown"}</dd>
          </div>
          <div>
            <dt>Image Ready</dt>
            <dd>{runnerStatus?.image_ready ? "yes" : "no"}</dd>
          </div>
          <div>
            <dt>Last Build Attempt</dt>
            <dd>{runnerStatus?.last_build_attempt ?? "none recorded"}</dd>
          </div>
          <div>
            <dt>Sandbox Status</dt>
            <dd>{sandboxStatusLabel(runnerStatus)}</dd>
          </div>
        </dl>
        {runnerStatus?.image_detail && <p className="muted">{runnerStatus.image_detail}</p>}
        {runnerStatus?.image_error && (
          <div className="run-detail">
            <h3>Last Image Build Error</h3>
            <pre>{runnerStatus.image_error}</pre>
          </div>
        )}
        {runnerStatus?.image_build_log && (
          <details className="run-detail">
            <summary>Image Build Log</summary>
            <pre>{runnerStatus.image_build_log}</pre>
          </details>
        )}
      </section>

      {error && <p className="error-text">{error}</p>}

      {runtimePermission && showRuntimeModal && (
        <PermissionRequestModal
          request={runtimePermission}
          title={`${displayName} runtime permissions`}
          subject="Approve these manifest permissions before installing or running this skill."
          isWorking={isWorking}
          approveLabel="Approve Runtime Permissions"
          denyLabel="Deny"
          onApprove={handleApproveRuntimePermissions}
          onDeny={handleDenyRuntimePermissions}
        />
      )}

      {validation && <ValidationResult validation={validation} />}

      <section className="detail-panel">
        <header className="page-header">
          <div>
            <h2>Agent Runs</h2>
            <p className="muted">Build and repair workflows are tracked as bounded, observable agent runs.</p>
            {agentRuns.some((run) => run.status === "succeeded") && (
              <p className="muted">
                Completed build usage: {new Intl.NumberFormat().format(
                  agentRuns.filter((run) => run.status === "succeeded").reduce((total, run) => total + run.total_tokens, 0),
                )} tokens
              </p>
            )}
          </div>
          <Link className="button-link secondary compact" to="/agent-runs">All Agent Runs</Link>
        </header>
        {agentRuns.length > 0 ? (
          <div className="run-list">
            {agentRuns.map((agentRun) => (
              <article key={agentRun.id} className="run-row">
                <div>
                  <strong>
                    <Link className="button-link secondary compact" to={`/agent-runs/${agentRun.id}`}>
                      Agent Run #{agentRun.id}
                    </Link>
                  </strong>
                  <span>{agentRun.run_type} / {agentRun.current_step ?? "none"}</span>
                  <span>{new Intl.NumberFormat().format(agentRun.total_tokens)} Codex tokens</span>
                  {agentRun.summary && <span>{agentRun.summary}</span>}
                </div>
                <span className={`badge status-${agentRun.status}`}>{agentRun.status}</span>
              </article>
            ))}
          </div>
        ) : (
          <p className="muted">No agent runs are linked to this skill yet.</p>
        )}
      </section>

      {isService && <section className="detail-panel">
        <header className="page-header">
          <div>
            <h2>Service Schedule</h2>
            <p className="muted">This service runs only through its required schedule. Manage timing, input, activation, and Run Now from Schedules.</p>
          </div>
          <Link className="button-link secondary compact" to="/schedules">Manage Schedule</Link>
        </header>
        {serviceSchedule ? (
          <dl className="detail-grid">
            <div><dt>Name</dt><dd>{formatDisplayName(serviceSchedule.name)}</dd></div>
            <div><dt>Status</dt><dd>{serviceSchedule.status}</dd></div>
            <div><dt>Type</dt><dd>{serviceSchedule.schedule_type}</dd></div>
            <div><dt>Timezone</dt><dd>{serviceSchedule.timezone}</dd></div>
            <div><dt>Next Run</dt><dd>{serviceSchedule.next_run_at ?? "not scheduled"}</dd></div>
            <div><dt>Last Result</dt><dd>{serviceSchedule.last_run_status ?? "never run"}</dd></div>
          </dl>
        ) : (
          <p className="error-text">This service is missing its required schedule.</p>
        )}
      </section>}

      <SkillFilesDisclosure files={files} />

      {isInstalled && isFunction && (
        <section className="detail-panel">
          <header className="page-header">
            <div>
              <h2>Run Input</h2>
              <p className="muted">Use valid JSON with double-quoted property names.</p>
            </div>
            <button
              type="button"
              className="secondary"
              onClick={() => setRunInput('{\n  "hello": "world"\n}')}
            >
              Reset Example
            </button>
          </header>
          <textarea
            value={runInput}
            onChange={(event) => setRunInput(event.target.value)}
            rows={8}
            aria-label="Run input JSON"
          />
          <div className="button-row">
            <button type="button" onClick={handleRun} disabled={!canRun || isRunning}>
              {isRunning ? "Running..." : "Run Function"}
            </button>
          </div>
        </section>
      )}

      {isInstalled && isFunction && (
        <>
          <section className="detail-panel">
            <h2>Output</h2>
            {pendingApproval ? (
              <div className="stack compact-action-stack">
                <p>Waiting for per-call approval #{pendingApproval.approval_id}.</p>
                <div className="button-row">
                  <Link className="button-link secondary compact" to="/approval-requests">
                    Open Invocation Approvals
                  </Link>
                </div>
              </div>
            ) : <RunOutput run={runOutput} />}
          </section>

          <section className="detail-panel">
            <h2>Run History</h2>
            <RunHistory runs={runs} />
          </section>
        </>
      )}
    </section>
  );
}
