import { NavLink, Navigate, Route, Routes } from "react-router-dom";

import ApprovalRequestsPage from "./pages/ApprovalRequestsPage";
import ChatPage from "./pages/ChatPage";
import MemoryPage from "./pages/MemoryPage";
import SkillDetailPage from "./pages/SkillDetailPage";
import SkillsPage from "./pages/SkillsPage";
import SchedulesPage from "./pages/SchedulesPage";

const navItems = [
  { to: "/chat", label: "Chat" },
  { to: "/memory", label: "Memory" },
  { to: "/skills", label: "Skills" },
  { to: "/schedules", label: "Schedules" },
  { to: "/approval-requests", label: "Approvals" },
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
          <Route path="/skills/:skillId" element={<SkillDetailPage />} />
          <Route path="/schedules" element={<SchedulesPage />} />
          <Route path="/approval-requests" element={<ApprovalRequestsPage />} />
        </Routes>
      </main>
    </div>
  );
}
