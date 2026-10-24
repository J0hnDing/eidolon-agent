import { useCallback, useEffect, useState } from "react";

import { api } from "../api/client";
import { usePolling } from "./usePolling";

/** Counts pending approvals across the three approval surfaces for shell navigation. */
export function usePendingApprovalCount(): number | null {
  const [count, setCount] = useState<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [permissions, invocations, proposals] = await Promise.all([
        api.listPermissionRequests({ status: "pending" }),
        api.listInvocationApprovals(),
        api.listAssistantProposals(),
      ]);
      setCount(
        permissions.length +
          invocations.filter((approval) => approval.decision_status === "pending").length +
          proposals.filter((proposal) => proposal.status === "pending").length,
      );
    } catch {
      // Keep the last known count while the backend is temporarily unavailable.
    }
  }, []);

  useEffect(() => {
    void refresh();
    window.addEventListener("eidolon:approvals-changed", refresh);
    return () => window.removeEventListener("eidolon:approvals-changed", refresh);
  }, [refresh]);

  usePolling(refresh, true, 30_000);
  return count;
}
