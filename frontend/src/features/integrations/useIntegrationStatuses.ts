import { useCallback, useEffect, useRef, useState } from "react";

import { api, AtlasIntegrationStatus, CodexMcpStatus } from "../../api/client";
import type { IntegrationId } from "./IntegrationNavigation";

export type IntegrationState = "connected" | "partial" | "disconnected" | "unavailable";
export type IntegrationStatus = { state: IntegrationState; description: string };
export type IntegrationStatuses = Partial<Record<IntegrationId, IntegrationStatus>>;

const unavailable: IntegrationStatus = { state: "unavailable", description: "Could not check connection status. Refresh to try again." };

export function summarizeConnections(connected: boolean[], description: string): IntegrationStatus {
  const count = connected.filter(Boolean).length;
  return {
    state: count === 0 ? "disconnected" : count === connected.length ? "connected" : "partial",
    description,
  };
}

function connectionStatus(connection: { connected: boolean; status: string }): IntegrationStatus {
  if (connection.status === "unavailable") return unavailable;
  return {
    state: connection.connected ? "connected" : ["pairing", "connecting", "reconnecting"].includes(connection.status) ? "partial" : "disconnected",
    description: `Connection: ${connection.status.replace(/_/g, " ")}.`,
  };
}

async function groupedConnections(requests: Array<Promise<{ connected: boolean; status: string }>>, labels: string[]): Promise<IntegrationStatus> {
  const results = await Promise.allSettled(requests);
  if (results.some((result) => result.status === "rejected" || result.value.status === "unavailable")) return unavailable;
  const connections = results.map((result) => (result as PromiseFulfilledResult<{ connected: boolean; status: string }>).value);
  const description = connections.map((connection, index) => `${labels[index]}: ${connection.status.replace(/_/g, " ")}`).join("; ");
  const summary = summarizeConnections(connections.map((connection) => connection.connected), description);
  if (summary.state === "disconnected" && connections.some((connection) => ["pairing", "connecting", "reconnecting"].includes(connection.status))) {
    summary.state = "partial";
  }
  return summary;
}

export function atlasConnectionStatus(status: AtlasIntegrationStatus): IntegrationStatus {
  if (status.error_type === "unavailable") return unavailable;
  if (status.running && status.initialized === true && status.locked === false) {
    return { state: "connected", description: "Atlas is running and unlocked." };
  }
  if (status.running) return { state: "partial", description: "Atlas is running but needs initialization or unlocking." };
  return { state: "disconnected", description: "Atlas is not running." };
}

export function codexConnectionStatus(status: CodexMcpStatus): IntegrationStatus {
  if (status.error_type === "unavailable") return unavailable;
  if (status.enabled && status.registered && status.config_matches && !status.error_type) {
    return { state: "connected", description: "Eidolon tools are enabled and registered in Codex." };
  }
  if (status.enabled || status.registered) return { state: "partial", description: "Codex tools need installation or repair." };
  return { state: "disconnected", description: "Eidolon tools are not enabled in Codex." };
}

function statusRequests(): Array<[IntegrationId, Promise<IntegrationStatus>]> {
  return [
    ["google", groupedConnections([api.getGoogleCalendarConnection(), api.getGmailConnection(), api.getGoogleDriveConnection()], ["Calendar", "Gmail", "Drive"])],
    ["github", api.getGitHubConnection().then(connectionStatus)],
    ["microsoft", api.getOutlookConnection().then(connectionStatus)],
    ["notion", api.getNotionConnection().then(connectionStatus)],
    ["quercus", api.getQuercusConnection().then(connectionStatus)],
    ["telegram", groupedConnections([api.getTelegramConnection(), api.getTelegramAgentConnection(), api.getTelegramObserverAgentConnection(), api.getTelegramAssistantAgentConnection()], ["Notification / Approval", "Act", "Observer", "Assistant"])],
    ["whatsapp", api.getWhatsAppConnection().then(connectionStatus)],
    ["wecom", api.getWeComConnection().then(connectionStatus)],
    ["atlas", api.getAtlasStatus().then(atlasConnectionStatus)],
    ["codex", api.getCodexMcpStatus().then(codexConnectionStatus)],
    ["browser-authentication", api.getBrowserAuthentication().then(({ identities }) => summarizeConnections(identities.map((identity) => identity.configured), "Stored login credentials only; this does not verify a live browser session."))],
    ["macro-data", Promise.all((["fred", "bls", "bea", "eia"] as const).map((provider) => api.getApiKeyConnection(provider))).then((statuses) => {
      if (statuses.some((status) => status.status === "unavailable")) return unavailable;
      return summarizeConnections(statuses.map((status) => status.configured), `${statuses.filter((status) => status.configured).length} of 4 API keys configured. Saved keys are not validated; BLS public access works without a key.`);
    })],
  ];
}

export function useIntegrationStatuses(enabled: boolean) {
  const [statuses, setStatuses] = useState<IntegrationStatuses>({});
  const [refreshing, setRefreshing] = useState(false);
  const generation = useRef(0);

  const refresh = useCallback(async () => {
    if (!enabled) return;
    const current = ++generation.current;
    setRefreshing(true);
    await Promise.allSettled(statusRequests().map(async ([id, request]) => {
      const status = await request.catch(() => unavailable);
      if (generation.current === current) setStatuses((previous) => ({ ...previous, [id]: status }));
    }));
    if (generation.current === current) setRefreshing(false);
  }, [enabled]);

  useEffect(() => {
    void refresh();
    return () => { generation.current += 1; };
  }, [refresh]);

  return { statuses, refreshing, refresh };
}
