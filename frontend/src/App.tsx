import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";

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

type NavIcon =
  | "chat"
  | "memory"
  | "skills"
  | "functions"
  | "apps"
  | "schedules"
  | "runs"
  | "approvals"
  | "settings";

const navGroups: Array<{
  label: string;
  items: Array<{ to: string; label: string; icon: NavIcon }>;
}> = [
  {
    label: "Workspace",
    items: [
      { to: "/chat", label: "Chat", icon: "chat" },
      { to: "/memory", label: "Memory", icon: "memory" },
    ],
  },
  {
    label: "Capabilities",
    items: [
      { to: "/skills", label: "Skills", icon: "skills" },
      { to: "/functions", label: "Functions", icon: "functions" },
      { to: "/apps", label: "Applications", icon: "apps" },
      { to: "/schedules", label: "Schedules", icon: "schedules" },
    ],
  },
  {
    label: "Control",
    items: [
      { to: "/agent-runs", label: "Agent Runs", icon: "runs" },
      { to: "/approval-requests", label: "Approvals", icon: "approvals" },
      { to: "/settings", label: "Settings", icon: "settings" },
    ],
  },
];

export default function App() {
  const location = useLocation();

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">Skip to content</a>
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            <span />
          </span>
          <div>
            <strong>Eidolon</strong>
            <span>Local intelligence</span>
          </div>
        </div>

        <nav className="nav-list" aria-label="Primary navigation">
          {navGroups.map((group) => (
            <div className="nav-group" key={group.label}>
              <p className="nav-group-label">{group.label}</p>
              <div className="nav-group-items">
                {group.items.map((item) => (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}
                  >
                    <NavigationIcon name={item.icon} />
                    <span>{item.label}</span>
                    <span className="nav-link-indicator" aria-hidden="true" />
                  </NavLink>
                ))}
              </div>
            </div>
          ))}
        </nav>

        <div className="sidebar-foot">
          <span className="local-status-dot" aria-hidden="true" />
          <span>
            <strong>Local-first</strong>
            <small>Your data stays under your control</small>
          </span>
        </div>
      </aside>

      <main className="main-content" id="main-content">
        <div className="route-stage" key={location.pathname}>
          <Routes>
            <Route path="/" element={<Navigate to="/chat" replace />} />
            <Route path="/chat" element={<ChatPage />} />
            <Route path="/act" element={<Navigate to="/chat" replace />} />
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
            <Route path="/settings/project" element={<UsageSettingsPage section="project" />} />
            <Route path="/settings/models" element={<UsageSettingsPage section="models" />} />
            <Route path="/settings/integrations" element={<UsageSettingsPage section="integrations" />} />
            <Route path="/settings/permissions" element={<UsageSettingsPage section="permissions" />} />
          </Routes>
        </div>
      </main>
    </div>
  );
}

function NavigationIcon({ name }: { name: NavIcon }) {
  const paths: Record<NavIcon, React.ReactNode> = {
    chat: <path d="M5 6.75A2.75 2.75 0 0 1 7.75 4h8.5A2.75 2.75 0 0 1 19 6.75v5.5A2.75 2.75 0 0 1 16.25 15H11l-4.5 3v-3.29A2.75 2.75 0 0 1 5 12.25v-5.5Z" />,
    memory: <path d="M8 5.5A2.5 2.5 0 0 1 10.5 3H16v15h-5.5A2.5 2.5 0 0 0 8 20.5v-15Zm0 0v15m0-15A2.5 2.5 0 0 0 5.5 3H4v15h1.5A2.5 2.5 0 0 1 8 20.5" />,
    skills: <path d="m12 3 2.15 4.35L19 8.06l-3.5 3.41.83 4.82L12 14.02l-4.33 2.27.83-4.82L5 8.06l4.85-.71L12 3Zm0 15.5v2" />,
    functions: <path d="M9 4H7.75A1.75 1.75 0 0 0 6 5.75v12.5C6 19.22 6.78 20 7.75 20H9m6-16h1.25C17.22 4 18 4.78 18 5.75v12.5c0 .97-.78 1.75-1.75 1.75H15M10 9l4 3-4 3" />,
    apps: <path d="M4 4h6v6H4V4Zm10 0h6v6h-6V4ZM4 14h6v6H4v-6Zm10 0h6v6h-6v-6Z" />,
    schedules: <path d="M7 3v3m10-3v3M4 9h16M6.5 5h11A2.5 2.5 0 0 1 20 7.5v10a2.5 2.5 0 0 1-2.5 2.5h-11A2.5 2.5 0 0 1 4 17.5v-10A2.5 2.5 0 0 1 6.5 5Zm2 8h3v3h-3v-3Z" />,
    runs: <path d="M5 4v16m0-3h4.25A2.75 2.75 0 0 0 12 14.25v-4.5A2.75 2.75 0 0 1 14.75 7H19m-3-3 3 3-3 3" />,
    approvals: <path d="M12 3 5 6v5c0 4.55 2.95 8.22 7 10 4.05-1.78 7-5.45 7-10V6l-7-3Zm-3 9 2 2 4-4" />,
    settings: <path d="M12 8.5a3.5 3.5 0 1 1 0 7 3.5 3.5 0 0 1 0-7Zm7.5 3.5-.06-.63 1.46-1.14-1.8-3.12-1.72.7a8.1 8.1 0 0 0-1.1-.64L16 5.33h-3.6l-.27 1.84c-.4.18-.76.4-1.1.64l-1.73-.7-1.8 3.12 1.46 1.14L8.9 12l.06.63-1.46 1.14 1.8 3.12 1.72-.7c.34.25.71.46 1.11.64l.27 1.84H16l.27-1.84c.4-.18.77-.4 1.11-.64l1.72.7 1.8-3.12-1.46-1.14.06-.63Z" />,
  };

  return (
    <svg className="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {paths[name]}
    </svg>
  );
}
