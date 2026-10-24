import { NavLink, Navigate, useLocation, useMatch } from "react-router-dom";

import "./records.css";

import AgentRunDetailPage from "./AgentRunDetailPage";
import AgentRunsPage from "./AgentRunsPage";
import ApprovalRequestsPage from "./ApprovalRequestsPage";

export default function ActivityPage() {
  const { pathname } = useLocation();
  const normalizedPath = pathname.replace(/\/$/, "") || "/";
  const runDetailMatch = useMatch("/activity/runs/:agentRunId");
  const isRunDetail = runDetailMatch !== null;
  const isApprovalPage = normalizedPath === "/activity/approvals";
  const isRunList = normalizedPath === "/activity/runs";

  if (normalizedPath === "/activity") {
    return <Navigate to="/activity/approvals" replace />;
  }

  if (!isApprovalPage && !isRunList && !isRunDetail) {
    return <Navigate to="/activity/approvals" replace />;
  }

  return (
    <section className="page stack activity-page records-page">
      <header className="page-header">
        <div>
          <h1>Activity</h1>
        </div>
      </header>

      <nav className="page-tabs activity-tabs" aria-label="Activity sections">
        <NavLink to="/activity/approvals" aria-current={isApprovalPage ? "page" : undefined}>
          Approvals
        </NavLink>
        <NavLink to="/activity/runs" aria-current={!isApprovalPage ? "page" : undefined}>
          Executions
        </NavLink>
      </nav>

      {isApprovalPage && <ApprovalRequestsPage embedded />}
      {isRunList && <AgentRunsPage embedded />}
      {isRunDetail && <AgentRunDetailPage embedded routeAgentRunId={runDetailMatch?.params.agentRunId} />}
    </section>
  );
}
