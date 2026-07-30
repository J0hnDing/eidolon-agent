import { NavLink, Navigate, Route, Routes } from "react-router-dom";

import ApprovalRequestsPage from "./pages/ApprovalRequestsPage";
import AgentRunDetailPage from "./pages/AgentRunDetailPage";
import AgentRunsPage from "./pages/AgentRunsPage";
import ChatPage from "./pages/ChatPage";
import FunctionsPage from "./pages/FunctionsPage";
import MemoryPage from "./pages/MemoryPage";
import SkillDetailPage from "./pages/SkillDetailPage";
import SkillsPage from "./pages/SkillsPage";
import SchedulesPage from "./pages/SchedulesPage";
import UsageSettingsPage from "./pages/UsageSettingsPage";
import WebAppPage from "./pages/WebAppPage";
import WebAppsPage from "./pages/WebAppsPage";

const navItems = [
  { to: "/chat", label: "Chat" },
  { to: "/memory", label: "Memory" },
  { to: "/skills", label: "Skills" },
  { to: "/functions", label: "Functions" },
  { to: "/apps", label: "Applications" },
  { to: "/schedules", label: "Schedules" },
  { to: "/agent-runs", label: "Agent Runs" },
  { to: "/approval-requests", label: "Approvals" },
  { to: "/settings/usage", label: "Settings" },
];

export default function App() {
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">E</span>
          <div>
            <strong>Eidolon</strong>
            <span>Local-first control plane</span>
          </div>
        </div>
        <nav className="nav-list" aria-label="Primary navigation">
          {navItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      </aside>

      <main className="main-content">
        <Routes>
          <Route path="/" element={<Navigate to="/chat" replace />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/memory" element={<MemoryPage />} />
          <Route path="/skills" element={<SkillsPage />} />
          <Route path="/functions" element={<FunctionsPage />} />
          <Route path="/apps" element={<WebAppsPage />} />
          <Route path="/apps/:skillId" element={<WebAppPage />} />
          <Route path="/skills/:skillId" element={<SkillDetailPage />} />
          <Route path="/schedules" element={<SchedulesPage />} />
          <Route path="/agent-runs" element={<AgentRunsPage />} />
          <Route path="/agent-runs/:agentRunId" element={<AgentRunDetailPage />} />
          <Route path="/approval-requests" element={<ApprovalRequestsPage />} />
          <Route path="/settings" element={<Navigate to="/settings/usage" replace />} />
          <Route path="/settings/usage" element={<UsageSettingsPage />} />
        </Routes>
      </main>
    </div>
  );
}
