import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { NavLink, Navigate, Route, Routes, useLocation, useParams } from "react-router-dom";

import AssistantPage, { AssistantDetailsPage } from "./pages/AssistantPage";
import { loadActiveConversationId, loadStoredConversations } from "./lib/chatStore";
import { usePendingApprovalCount } from "./lib/usePendingApprovalCount";
import { PageErrorBoundary } from "./components/PageErrorBoundary";
import { AppIcon } from "./components/AppIcon";
import { CommandMenu } from "./components/CommandMenu";
import eidolonLogo from "../resource/logo/eidolon-mark.svg";
import "./product-shell.css";

const ActivityPage = lazy(() => import("./pages/ActivityPage"));
const AgentsPage = lazy(() => import("./pages/AgentsPage"));
const FunctionsSettingsPage = lazy(() => import("./pages/FunctionsSettingsPage"));
const IntegrationsPage = lazy(() => import("./pages/IntegrationsPage"));
const MemoryPage = lazy(() => import("./pages/MemoryPage"));
const SkillBuildPage = lazy(() => import("./pages/SkillBuildPage"));
const SkillDetailPage = lazy(() => import("./pages/SkillDetailPage"));
const SkillsPage = lazy(() => import("./pages/SkillsPage"));
const SchedulesPage = lazy(() => import("./pages/SchedulesPage"));
const UsageSettingsPage = lazy(() => import("./pages/UsageSettingsPage"));
const WebAppPage = lazy(() => import("./pages/WebAppPage"));
const WebAppsPage = lazy(() => import("./pages/WebAppsPage"));

type NavIcon = "chat" | "agents" | "memory" | "skills" | "functions" | "apps" | "schedules" | "runs" | "approvals" | "settings";
const destinations: Array<{ to: string; label: string; icon: NavIcon }> = [
  { to: "/assistant", label: "Assistant", icon: "chat" },
  { to: "/agents", label: "Agents", icon: "agents" },
  { to: "/apps", label: "Applications", icon: "apps" },
  { to: "/skills", label: "Skills", icon: "skills" },
  { to: "/automations", label: "Automations", icon: "schedules" },
  { to: "/memory", label: "Memory", icon: "memory" },
  { to: "/activity", label: "Activity", icon: "runs" },
];

export default function App() {
  const location = useLocation();
  const [navOpen, setNavOpen] = useState(false);
  const [commandOpen, setCommandOpen] = useState(false);
  const [compactNav, setCompactNav] = useState(() => window.matchMedia?.("(max-width: 800px)").matches ?? false);
  const sidebar = useRef<HTMLElement>(null);
  const toggle = useRef<HTMLButtonElement>(null);
  const pendingCount = usePendingApprovalCount();

  useEffect(() => {
    function onShortcut(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); setNavOpen(false); setCommandOpen((open) => !open); }
    }
    window.addEventListener("keydown", onShortcut);
    return () => window.removeEventListener("keydown", onShortcut);
  }, []);

  useEffect(() => { setNavOpen(false); }, [location.pathname]);
  useEffect(() => {
    const media = window.matchMedia?.("(min-width: 801px)");
    const onResize = () => { setCompactNav(!media?.matches); if (media?.matches) setNavOpen(false); };
    media?.addEventListener("change", onResize);
    return () => media?.removeEventListener("change", onResize);
  }, []);
  useEffect(() => {
    if (!navOpen) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    sidebar.current?.querySelector<HTMLElement>("a")?.focus();
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") { event.preventDefault(); setNavOpen(false); }
      if (event.key !== "Tab") return;
      const links = Array.from(sidebar.current?.querySelectorAll<HTMLElement>("a, button") ?? []);
      const first = links[0], last = links[links.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => { document.body.style.overflow = previousOverflow; window.removeEventListener("keydown", onKeyDown); toggle.current?.focus(); };
  }, [navOpen]);

  return <div className={`app-shell ${navOpen ? "nav-open" : ""}`}>
    <a className="skip-link" href="#main-content">Skip to content</a>
    {navOpen && <button type="button" className="sidebar-backdrop" aria-label="Close navigation" tabIndex={-1} onClick={() => setNavOpen(false)} />}
    <aside ref={sidebar} id="app-navigation" className={`sidebar ${navOpen ? "is-open" : ""}`} aria-hidden={compactNav && !navOpen ? true : undefined} {...(compactNav && !navOpen ? { inert: "" } : {})}>
      <div className="sidebar-brand-row"><NavLink to="/assistant" className="brand" aria-label="Eidolon home"><span className="brand-mark" aria-hidden="true"><img src={eidolonLogo} alt="" /></span><strong>Eidolon</strong></NavLink><div className="sidebar-brand-actions"><button className="sidebar-search ghost" type="button" aria-label="Go to…" title="Go to… (Ctrl K)" onClick={() => { setNavOpen(false); setCommandOpen(true); }}><AppIcon name="search" size={16} /></button><button type="button" className="mobile-nav-toggle ghost" onClick={() => setNavOpen(false)} aria-label="Close navigation"><AppIcon name="close" size={18} /></button></div></div>
      <nav className="nav-list" aria-label="Primary navigation">{[
        { title: "Personal", items: [destinations[0], destinations[1], destinations[5]] },
        { title: "Workspace", items: [destinations[2], destinations[3], destinations[4], destinations[6]] },
      ].map((group) => <div className="nav-group" key={group.title}>{group.title === "Workspace" && <p className="nav-group-label">Workspace</p>}<div className="nav-group-items">{group.items.map((item) => <NavLink key={item.to} to={item.to} className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`}>
        <AppIcon name={item.to === "/activity" ? "activity" : item.icon} size={18} /><span>{item.label}</span>{item.to === "/activity" && pendingCount !== null && pendingCount > 0 && <span className="nav-badge" aria-label={`${pendingCount} pending approvals`}>{pendingCount > 99 ? "99+" : pendingCount}</span>}
      </NavLink>)}</div></div>)}</nav>
      <div className="sidebar-bottom"><nav className="nav-settings" aria-label="Settings navigation"><NavLink to="/settings" className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`}><NavigationIcon name="settings" /><span>Settings</span></NavLink></nav>
      </div>
    </aside>
    <main className="main-content" id="main-content" tabIndex={-1} aria-hidden={navOpen ? true : undefined} {...(navOpen ? { inert: "" } : {})}>
      <div className="app-content-header"><button ref={toggle} type="button" className="mobile-nav-toggle ghost" aria-label="Open navigation" title="Open navigation" aria-expanded={navOpen} aria-controls="app-navigation" onClick={() => setNavOpen((open) => !open)}><AppIcon name="panel" /></button></div>
      <div className="route-stage" key={location.pathname}>
        <PageErrorBoundary><Suspense fallback={<section className="page"><p className="muted" role="status">Loading page…</p></section>}><Routes>
          <Route path="/" element={<Navigate to="/assistant" replace />} />
          <Route path="/assistant" element={<AssistantPage />} />
          <Route path="/assistant/details" element={<AssistantDetailsPage />} />
          <Route path="/chat" element={<LegacyChatRedirect />} />
          <Route path="/act" element={<Navigate to="/agents/act" replace />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/agents/assistant" element={<Navigate to="/assistant" replace />} />
          <Route path="/agents/:agentId" element={<AgentsPage />} />
          <Route path="/memory" element={<MemoryPage />} />
          <Route path="/skills" element={<SkillsPage />} />
          <Route path="/skills/build" element={<SkillBuildPage />} />
          <Route path="/skills/:skillId" element={<SkillDetailPage />} />
          <Route path="/apps" element={<WebAppsPage />} />
          <Route path="/apps/:skillId" element={<WebAppPage />} />
          <Route path="/automations" element={<SchedulesPage />} />
          <Route path="/schedules" element={<Navigate to="/automations" replace />} />
          <Route path="/activity/*" element={<ActivityPage />} />
          <Route path="/agent-runs" element={<Navigate to="/activity/runs" replace />} />
          <Route path="/agent-runs/:agentRunId" element={<LegacyRunRedirect />} />
          <Route path="/approval-requests" element={<Navigate to="/activity/approvals" replace />} />
          <Route path="/functions" element={<Navigate to="/settings/functions" replace />} />
          <Route path="/settings" element={<Navigate to="/settings/appearance" replace />} />
          <Route path="/settings/appearance" element={<UsageSettingsPage section="appearance" />} />
          <Route path="/settings/usage" element={<UsageSettingsPage />} />
          <Route path="/settings/project" element={<UsageSettingsPage section="project" />} />
          <Route path="/settings/models" element={<UsageSettingsPage section="models" />} />
          <Route path="/settings/integrations" element={<IntegrationsPage />} />
          <Route path="/settings/integrations/:integrationId" element={<IntegrationsPage />} />
          <Route path="/settings/permissions" element={<UsageSettingsPage section="permissions" />} />
          <Route path="/settings/functions" element={<FunctionsSettingsPage />} />
          <Route path="*" element={<section className="page stack"><h1>Page not found</h1><p className="muted">This page may have moved.</p><NavLink className="button-link" to="/assistant">Return to Assistant</NavLink></section>} />
        </Routes></Suspense></PageErrorBoundary>
      </div>
    </main>
    {commandOpen && <CommandMenu onClose={() => setCommandOpen(false)} />}
  </div>;
}

function LegacyChatRedirect() {
  const conversations = loadStoredConversations();
  const active = conversations.find((item) => item.id === loadActiveConversationId(conversations));
  const target = active?.mode === "project" ? "/skills/build" : active?.mode === "act" || active?.mode === "observer"
    ? `/agents/${active.mode}${active.actSessionId ? `?session=${active.actSessionId}` : ""}` : "/assistant";
  return <Navigate to={target} replace />;
}
function LegacyRunRedirect() { const { agentRunId } = useParams(); return <Navigate to={`/activity/runs/${agentRunId}`} replace />; }
function NavigationIcon({ name }: { name: NavIcon }) {
  const paths: Record<NavIcon, React.ReactNode> = {
    chat: <path d="M5 6.75A2.75 2.75 0 0 1 7.75 4h8.5A2.75 2.75 0 0 1 19 6.75v5.5A2.75 2.75 0 0 1 16.25 15H11l-4.5 3v-3.29A2.75 2.75 0 0 1 5 12.25v-5.5Z" />,
    agents: <path d="M8.5 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm7-1a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5ZM3.5 19v-2.25A3.75 3.75 0 0 1 7.25 13h2.5a3.75 3.75 0 0 1 3.75 3.75V19m.5-6h1.75A3.25 3.25 0 0 1 19 16.25V19" />,
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
