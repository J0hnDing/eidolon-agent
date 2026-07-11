import { NavLink, Navigate, Route, Routes } from "react-router-dom";

import ApprovalRequestsPage from "./pages/ApprovalRequestsPage";
import AgentRunDetailPage from "./pages/AgentRunDetailPage";
import AgentRunsPage from "./pages/AgentRunsPage";
import ChatPage from "./pages/ChatPage";
import MemoryPage from "./pages/MemoryPage";
import SkillDetailPage from "./pages/SkillDetailPage";
import SkillsPage from "./pages/SkillsPage";
import SchedulesPage from "./pages/SchedulesPage";
import ToolDetailPage from "./pages/ToolDetailPage";
import ToolsPage from "./pages/ToolsPage";
import UsageSettingsPage from "./pages/UsageSettingsPage";

const navItems = [
  { to: "/chat", label: "Chat" },
  { to: "/memory", label: "Memory" },
  { to: "/skills", label: "Skills" },
  { to: "/tools", label: "Tools" },
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
          <span className="brand-mark">PA</span>
          <div>
            <strong>Personal Agent</strong>
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
          <Route path="/tools" element={<ToolsPage />} />
          <Route path="/tools/:skillId" element={<ToolDetailPage />} />
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
