import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import PermissionRequestModal from "../components/PermissionRequestModal";
import { ApprovalRequest, ProposedSkillValidation, Skill, SkillFile, SkillRun, api } from "../api/client";

export default function SkillDetailPage() {
  const { skillId } = useParams();
  const [skill, setSkill] = useState<Skill | null>(null);
  const [runs, setRuns] = useState<SkillRun[]>([]);
  const [files, setFiles] = useState<SkillFile[]>([]);
  const [validation, setValidation] = useState<ProposedSkillValidation | null>(null);
  const [runtimePermission, setRuntimePermission] = useState<ApprovalRequest | null>(null);
  const [showRuntimeModal, setShowRuntimeModal] = useState(false);
  const [runInput, setRunInput] = useState("{}");
  const [isLoading, setIsLoading] = useState(true);
  const [isRunning, setIsRunning] = useState(false);
  const [isWorking, setIsWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const latestRun = runs[0] ?? null;

  useEffect(() => {
    async function loadSkillDetail() {
      if (!skillId) return;
      setIsLoading(true);
      setError(null);
      try {
        const id = Number(skillId);
        const [loadedSkill, loadedRuns, loadedFiles, permissionRequests] = await Promise.all([
          api.getSkill(id),
          api.listSkillRuns(id),
          api.listSkillFiles(id),
          api.listPermissionRequests({ skill_id: id, request_scope: "runtime" }),
        ]);
        setSkill(loadedSkill);
        setRuns(loadedRuns);
        setFiles(loadedFiles);
        setRuntimePermission(permissionRequests[0] ?? null);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not load skill");
      } finally {
        setIsLoading(false);
      }
    }
    loadSkillDetail();
  }, [skillId]);

  async function handleRun() {
    if (!skill) return;
    if (runtimePermission?.status !== "approved") {
      await handleReviewRuntimePermissions(true);
      return;
    }
    setIsRunning(true);
    setError(null);
    try {
      const parsedInput = JSON.parse(runInput);
      if (parsedInput === null || Array.isArray(parsedInput) || typeof parsedInput !== "object") {
        throw new Error("Run input must be a JSON object");
      }
      const run = await api.runSkill(skill.id, parsedInput as Record<string, unknown>);
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
    if (runtimePermission?.status !== "approved") {
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
      const rejected = await api.rejectSkill(skill.id);
      setSkill(rejected);
      setFiles([]);
      setValidation(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not reject skill");
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
    if (!skill) return;
    setIsWorking(true);
    setError(null);
    try {
      const request = await api.approveRuntimePermissions(skill.id);
      setRuntimePermission(request);
      setShowRuntimeModal(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not approve runtime permissions");
    } finally {
      setIsWorking(false);
    }
  }

  async function handleDenyRuntimePermissions() {
    if (!skill) return;
    setIsWorking(true);
    setError(null);
    try {
      const request = await api.denyRuntimePermissions(skill.id);
      setRuntimePermission(request);
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
        <Link to="/skills">Back to skills</Link>
        <p className="error-text">{error ?? "Skill not found"}</p>
      </section>
    );
  }

  const isExecutable = skill.skill_type === "automation" || skill.skill_type === "hybrid";
  const isInstalledExecutable = skill.status === "installed" && isExecutable;
  const runtimeApproved = runtimePermission?.status === "approved";
  const canRun = isInstalledExecutable && skill.enabled && runtimeApproved;
  const isProposed = skill.status === "proposed";

  return (
    <section className="page stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">Skill Detail</p>
          <h1>{skill.name}</h1>
        </div>
        <Link to="/skills">Back to skills</Link>
      </header>

      <section className="detail-panel">
        <p>{skill.description}</p>
        <dl className="detail-grid">
          <div>
            <dt>Skill Type</dt>
            <dd>{skill.skill_type}</dd>
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
        </dl>
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
          <span className={`badge ${runtimePermission ? `risk-${runtimePermission.risk_level}` : ""}`}>
            {runtimePermission ? runtimePermission.status : "not analyzed"}
          </span>
        </header>
        {Boolean(runtimePermission?.reason_json?.permission_expansion) && (
          <p className="error-text">Permission expansion detected. Review the generated manifest before approving.</p>
        )}
        <div className="button-row">
          <button type="button" className="secondary" onClick={() => handleReviewRuntimePermissions(true)} disabled={isWorking}>
            Review Runtime Permissions
          </button>
        </div>
      </section>

      {isProposed ? (
        <div className="button-row">
          <button type="button" onClick={handleValidate} disabled={isWorking}>
            Validate/Test
          </button>
          <button type="button" onClick={handleInstall} disabled={isWorking}>
            Install
          </button>
          <button type="button" className="danger" onClick={handleReject} disabled={isWorking}>
            Reject/Delete
          </button>
        </div>
      ) : (
        <div className="button-row">
          {canRun && (
            <button type="button" onClick={handleRun} disabled={isRunning}>
              {isRunning ? "Running..." : "Run"}
            </button>
          )}
          {isInstalledExecutable && skill.enabled && !runtimeApproved && (
            <button type="button" onClick={() => handleReviewRuntimePermissions(true)} disabled={isWorking}>
              Review Before Run
            </button>
          )}
          {isInstalledExecutable && !skill.enabled && (
            <button type="button" disabled>
              Run Disabled
            </button>
          )}
          <button type="button" onClick={handleToggleEnabled} disabled={isWorking}>
            {skill.enabled ? "Disable" : "Enable"}
          </button>
          <button type="button" className="danger" disabled>
            Delete
          </button>
        </div>
      )}

      {error && <p className="error-text">{error}</p>}

      {runtimePermission && showRuntimeModal && (
        <PermissionRequestModal
          request={runtimePermission}
          title={`${skill.name} runtime permissions`}
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
        <h2>Skill Files</h2>
        {files.length > 0 ? (
          <div className="file-list">
            {files.map((file) => (
              <article key={file.path}>
                <h3>{file.path}</h3>
                <pre>{file.content}</pre>
              </article>
            ))}
          </div>
        ) : (
          <p className="muted">No readable skill files found.</p>
        )}
      </section>

      {isInstalledExecutable && (
        <section className="detail-panel">
          <h2>Run Input</h2>
          <textarea
            value={runInput}
            onChange={(event) => setRunInput(event.target.value)}
            rows={8}
            aria-label="Run input JSON"
          />
        </section>
      )}

      {isInstalledExecutable && (
        <>
          <section className="detail-panel">
            <h2>Latest Run</h2>
            {latestRun ? (
              <RunDetail run={latestRun} />
            ) : (
              <p className="muted">No runs recorded yet.</p>
            )}
          </section>

          <section className="detail-panel">
            <h2>Run History</h2>
            {runs.length > 0 ? (
              <div className="run-list">
                {runs.map((run) => (
                  <article key={run.id} className="run-row">
                    <div>
                      <strong>Run #{run.id}</strong>
                      <span>
                        {run.started_at ? new Date(run.started_at).toLocaleString() : "not started"}
                      </span>
                    </div>
                    <span className={`badge run-${run.status}`}>{run.status}</span>
                  </article>
                ))}
              </div>
            ) : (
              <p className="muted">No run history yet.</p>
            )}
          </section>
        </>
      )}
    </section>
  );
}

function ValidationResult({ validation }: { validation: ProposedSkillValidation }) {
  return (
    <section className="detail-panel">
      <h2>Validation Result</h2>
      <dl className="detail-grid">
        <div>
          <dt>Status</dt>
          <dd>{validation.ok ? "passed" : "failed"}</dd>
        </div>
        <div>
          <dt>Manifest</dt>
          <dd>{validation.manifest_valid ? "valid" : "invalid"}</dd>
        </div>
        <div>
          <dt>Tests</dt>
          <dd>
            {validation.tests_run
              ? validation.tests_passed
                ? "passed"
                : "failed"
              : "not run"}
          </dd>
        </div>
      </dl>
      {validation.error_message && (
        <div className="run-detail">
          <h3>Error</h3>
          <pre>{validation.error_message}</pre>
        </div>
      )}
      {validation.warnings.length > 0 && (
        <div className="run-detail">
          <h3>Warnings</h3>
          <pre>{validation.warnings.join("\n")}</pre>
        </div>
      )}
      {(validation.stdout || validation.stderr) && (
        <div className="run-detail">
          <h3>Test Output</h3>
          <pre>{validation.stdout || "No stdout captured."}</pre>
          <h3>Test Errors</h3>
          <pre>{validation.stderr || "No stderr captured."}</pre>
        </div>
      )}
    </section>
  );
}

function RunDetail({ run }: { run: SkillRun }) {
  return (
    <div className="run-detail">
      <dl className="detail-grid">
        <div>
          <dt>Status</dt>
          <dd>{run.status}</dd>
        </div>
        <div>
          <dt>Exit Code</dt>
          <dd>{run.exit_code ?? "none"}</dd>
        </div>
        <div>
          <dt>Started</dt>
          <dd>{run.started_at ? new Date(run.started_at).toLocaleString() : "not started"}</dd>
        </div>
        <div>
          <dt>Ended</dt>
          <dd>{run.ended_at ? new Date(run.ended_at).toLocaleString() : "not ended"}</dd>
        </div>
      </dl>

      {run.error_message && (
        <div>
          <h3>Error</h3>
          <pre>{run.error_message}</pre>
        </div>
      )}
      {run.output_json && (
        <div>
          <h3>Output JSON</h3>
          <pre>{JSON.stringify(run.output_json, null, 2)}</pre>
        </div>
      )}
      <div>
        <h3>Stdout</h3>
        <pre>{run.stdout || "No stdout captured."}</pre>
      </div>
      <div>
        <h3>Stderr</h3>
        <pre>{run.stderr || "No stderr captured."}</pre>
      </div>
    </div>
  );
}
