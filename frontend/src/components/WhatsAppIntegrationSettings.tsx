import { useCallback, useEffect, useState } from "react";

import { api, WhatsAppIntegrationStatus } from "../api/client";
import { Modal } from "./Modal";

type PendingAction = "cache" | "disconnect" | null;

export default function WhatsAppIntegrationSettings() {
  const [status, setStatus] = useState<WhatsAppIntegrationStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [working, setWorking] = useState(false);
  const [pendingAction, setPendingAction] = useState<PendingAction>(null);

  const refresh = useCallback(async (pairing = false) => {
    setError(null);
    try {
      setStatus(pairing ? await api.getWhatsAppPairingStatus() : await api.getWhatsAppConnection());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load WhatsApp status");
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  useEffect(() => {
    if (!status || !["pairing", "connecting", "reconnecting"].includes(status.status)) return undefined;
    const interval = window.setInterval(() => { void refresh(true); }, 2_000);
    return () => window.clearInterval(interval);
  }, [refresh, status]);

  async function runAction(action: "pair" | "reconnect" | "cache" | "disconnect") {
    setWorking(true);
    setError(null);
    setNotice(null);
    try {
      const next = action === "pair" ? await api.startWhatsAppPairing()
        : action === "reconnect" ? await api.reconnectWhatsApp()
          : action === "cache" ? await api.clearWhatsAppCache()
            : await api.removeWhatsAppConnection();
      setStatus(action === "pair" && !next.connected ? await api.getWhatsAppPairingStatus() : next);
      setPendingAction(null);
      if (next.error_type) setNotice(action === "disconnect" || action === "cache"
        ? "WhatsApp cleanup is incomplete. Retry cleanup to finish removing local pairing data."
        : `WhatsApp setup needs attention: ${next.error_type.replace(/_/g, " ")}.`);
      else if (action === "cache") setNotice("The cached WhatsApp history was cleared.");
      else if (action === "disconnect") setNotice(next.remote_logout_failed
        ? "WhatsApp was disconnected locally. Remove Eidolon from Linked Devices in WhatsApp to finish revoking the session."
        : "WhatsApp was disconnected.");
    } catch (err) {
      await refresh();
      setError(err instanceof Error ? err.message : "WhatsApp action failed");
    } finally {
      setWorking(false);
    }
  }

  const configured = Boolean(status && status.status !== "disconnected");
  const cleanupPending = status?.error_type === "cleanup_pending";

  return (
    <section className="detail-panel stack" aria-labelledby="whatsapp-heading">
      <div>
        <h2 id="whatsapp-heading">WhatsApp</h2>
        <p className="muted">Link one WhatsApp account. Eidolon caches available message text for 30 days so approved functions can search it. Incoming messages do not start agent conversations.</p>
      </div>
      <dl className="detail-grid">
        <div><dt>Status</dt><dd>{status ? whatsappStatusLabel(status.status, status.connected) : "Checking…"}</dd></div>
        <div><dt>Account</dt><dd>{status?.account_login ?? status?.account_id ?? "Not paired"}</dd></div>
        <div><dt>History sync</dt><dd>{status?.sync_status.replace(/_/g, " ") ?? "Unknown"}</dd></div>
        <div><dt>Available from</dt><dd>{formatDate(status?.oldest_message_at) ?? "No cached messages"}</dd></div>
        <div><dt>Retention cutoff</dt><dd>{formatDate(status?.retention_cutoff) ?? "30 days"}</dd></div>
        <div><dt>Last synchronized</dt><dd>{formatDate(status?.last_synced_at) ?? "Not yet"}</dd></div>
      </dl>
      <p className="muted">The cache contains only history WhatsApp supplies to the linked device; it may be incomplete. Outbound messages require Eidolon’s per-call approval.</p>
      {status?.qr_data_url && (
        <div className="settings-subsection stack" aria-label="WhatsApp pairing QR code">
          <h3>Scan to link WhatsApp</h3>
          <p>On your phone, open WhatsApp → Settings → Linked Devices → Link a Device, then scan this code.</p>
          <img src={status.qr_data_url} alt="WhatsApp linking QR code" width="240" height="240" />
        </div>
      )}
      {status?.error_type && <p className="error-text">Connection status: {status.error_type.replace(/_/g, " ")}</p>}
      {error && <p className="error-text" role="alert">{error}</p>}
      {notice && <p className="success-text" role="status">{notice}</p>}
      <div className="button-row">
        <button type="button" className="secondary" onClick={() => void refresh(status?.status === "pairing")} disabled={working}>Refresh</button>
        {!status?.connected && !cleanupPending && (!configured || status?.account_id) && <button type="button" onClick={() => void runAction("pair")} disabled={working || status?.status === "pairing"}>{status?.status === "pairing" ? "Waiting for scan…" : status?.account_id ? "Pair WhatsApp again" : "Pair WhatsApp"}</button>}
        {status?.account_id && !cleanupPending && <button type="button" className="secondary" onClick={() => void runAction("reconnect")} disabled={working}>Reconnect</button>}
        {status?.account_id && !cleanupPending && <button type="button" className="secondary" onClick={() => setPendingAction("cache")} disabled={working}>Clear cached history</button>}
        {configured && <button type="button" className="secondary" onClick={() => setPendingAction("disconnect")} disabled={working}>{cleanupPending ? "Retry cleanup" : "Disconnect WhatsApp"}</button>}
      </div>
      {pendingAction && (
        <Modal title={pendingAction === "cache" ? "Clear WhatsApp history?" : "Disconnect WhatsApp?"} onClose={() => { if (!working) setPendingAction(null); }}>
          <div className="stack">
            <p>{pendingAction === "cache" ? "This permanently removes cached WhatsApp messages, contacts, and chat details from Eidolon. Pairing will remain active." : status?.status === "pairing" ? "This cancels the pending WhatsApp pairing and removes any saved session and cached data from Eidolon." : cleanupPending ? "This retries removing the saved WhatsApp session and cached data from Eidolon." : "This removes the linked session and cached WhatsApp history from Eidolon. You may also need to remove the linked device from WhatsApp on your phone."}</p>
            <div className="button-row">
              <button type="button" className="danger" onClick={() => void runAction(pendingAction)} disabled={working}>{working ? "Working…" : pendingAction === "cache" ? "Clear history" : cleanupPending ? "Retry cleanup" : "Disconnect"}</button>
              <button type="button" className="secondary" onClick={() => setPendingAction(null)} disabled={working}>Cancel</button>
            </div>
          </div>
        </Modal>
      )}
    </section>
  );
}

function formatDate(value: string | null | undefined): string | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toLocaleString();
}

function whatsappStatusLabel(value: string, connected: boolean): string {
  if (connected) return "Connected";
  return value.replace(/_/g, " ").replace(/^./, (character) => character.toUpperCase());
}
