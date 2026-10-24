import { NavLink, Navigate, useParams, useSearchParams } from "react-router-dom";
import { AgentConfiguration } from "../components/AgentConfiguration";
import ChatPage from "./ChatPage";
import { AppIcon } from "../components/AppIcon";

export default function AgentsPage() {
  const { agentId: routeAgentId } = useParams();
  const [params, setParams] = useSearchParams();
  const agentId = routeAgentId ?? "act";
  if (agentId === "assistant") return <Navigate to="/assistant" replace />;
  if (agentId !== "act" && agentId !== "observer") return <section className="page stack"><h1>Agent not found</h1><NavLink to="/agents">Return to Agents</NavLink></section>;
  const configuration = params.get("view") === "configuration";
  return <section className="page stack agents-page">
    <header className="page-header"><div><h1>Agents</h1></div><NavLink className="button-link secondary compact" to="/assistant">Open Assistant <span aria-hidden="true">↗</span></NavLink></header>
    <div className="agent-workspace-toolbar">
    <nav className="page-tabs agent-type-tabs" aria-label="Choose an agent">
      <NavLink to="/agents/act" aria-label="Act" className={agentId === "act" ? "active" : ""}><span className="agent-glyph act"><AppIcon name="agents" /></span><span className="agent-switch-copy"><strong>Act</strong></span></NavLink>
      <NavLink to="/agents/observer" aria-label="Observer" className={agentId === "observer" ? "active" : ""}><span className="agent-glyph observer"><AppIcon name="eye" /></span><span className="agent-switch-copy"><strong>Observer</strong></span></NavLink>
    </nav>
      <div className="page-tabs agent-local-nav" role="group" aria-label="Agent view">
        <button type="button" className={!configuration ? "active" : "secondary"} aria-pressed={!configuration} onClick={() => { const next = new URLSearchParams(params); next.delete("view"); setParams(next); }}>Conversation</button>
        <button type="button" className={configuration ? "active" : "secondary"} aria-pressed={configuration} onClick={() => { const next = new URLSearchParams(params); next.set("view", "configuration"); setParams(next); }}>Configuration</button>
      </div>
    </div>
    <div className="agent-conversation-view" hidden={configuration}><ChatPage key={agentId} surface="agent" agentId={agentId} /></div>
    {configuration && <div className="agent-configuration-view"><AgentConfiguration key={agentId} agentId={agentId} /></div>}
  </section>;
}
