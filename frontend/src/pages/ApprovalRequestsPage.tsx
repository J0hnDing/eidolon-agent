import { useEffect, useState } from "react";

import { ApprovalRequest, api } from "../api/client";

export default function ApprovalRequestsPage() {
  const [requests, setRequests] = useState<ApprovalRequest[]>([]);
  const [selected, setSelected] = useState<ApprovalRequest | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isWorking, setIsWorking] = useState(false);

  async function loadRequests() {
    setError(null);
    try {
      const loaded = await api.listPermissionRequests();
      setRequests(loaded);
      setSelected((current) => loaded.find((item) => item.id === current?.id) ?? loaded[0] ?? null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load approval requests");
    }
  }

  useEffect(() => {
    loadRequests();
  }, []);

  async function decide(action: "approve" | "deny") {
    if (!selected) return;
    setIsWorking(true);
    setError(null);
    try {
      const updated =
        action === "approve"
          ? await api.approvePermissionRequest(selected.id)
          : await api.denyPermissionRequest(selected.id);
      setSelected(updated);
      await loadRequests();
    } catch (err) {
      setError(err instanceof Error ? err.message : `Could not ${action} request`);
    } finally {
      setIsWorking(false);
    }
  }

  return (
    <section className="page stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">Risk control</p>
          <h1>Approval Requests</h1>
        </div>
      </header>

      {error && <p className="error-text">{error}</p>}

      <section className="section-grid">
        <div className="detail-panel">
          <h2>Requests</h2>
          <div className="run-list">
            {requests.map((request) => (
              <button
                key={request.id}
                type="button"
                className="secondary"
                onClick={() => setSelected(request)}
              >
                #{request.id} {request.request_scope} {request.status}
              </button>
            ))}
            {requests.length === 0 && <p className="muted">No approval requests yet.</p>}
          </div>
        </div>

        <div className="detail-panel">
          <h2>Details</h2>
          {selected ? (
            <div className="run-detail">
              <dl className="detail-grid">
                <div>
                  <dt>Scope</dt>
                  <dd>{selected.request_scope}</dd>
                </div>
                <div>
                  <dt>Status</dt>
                  <dd>{selected.status}</dd>
                </div>
                <div>
                  <dt>Risk</dt>
                  <dd>{selected.risk_level}</dd>
                </div>
                <div>
                  <dt>Type</dt>
                  <dd>{selected.request_type}</dd>
                </div>
              </dl>
              <p>{selected.user_explanation || selected.reason}</p>
              <pre>{JSON.stringify(selected, null, 2)}</pre>
              <div className="button-row">
                <button
                  type="button"
                  onClick={() => decide("approve")}
                  disabled={isWorking || selected.status !== "pending" || selected.risk_level === "blocked"}
                >
                  Approve
                </button>
                <button
                  type="button"
                  className="secondary"
                  onClick={() => decide("deny")}
                  disabled={isWorking || selected.status !== "pending"}
                >
                  Deny
                </button>
              </div>
            </div>
          ) : (
            <p className="muted">Select an approval request.</p>
          )}
        </div>
      </section>
    </section>
  );
}
