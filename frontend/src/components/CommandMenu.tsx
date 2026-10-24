import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { AppIcon } from "./AppIcon";
import { Modal } from "./Modal";

const destinations = [
  ["Assistant", "/assistant", "chat", "Personal"], ["Assistant details", "/assistant/details", "panel", "Personal"], ["Agents", "/agents", "agents", "Personal"],
  ["Memory", "/memory", "memory", "Personal"], ["Applications", "/apps", "apps", "Workspace"],
  ["Skills", "/skills", "skills", "Workspace"], ["Build a skill", "/skills/build", "plus", "Workspace"],
  ["Automations", "/automations", "schedules", "Workspace"], ["Approvals", "/activity/approvals", "check", "Activity"],
  ["Executions", "/activity/runs", "activity", "Activity"], ["Appearance", "/settings/appearance", "settings", "Settings"],
  ["Usage", "/settings/usage", "activity", "Settings"], ["Models", "/settings/models", "agents", "Settings"],
  ["Integrations", "/settings/integrations", "apps", "Settings"], ["Permissions", "/settings/permissions", "shield", "Settings"],
  ["Functions", "/settings/functions", "code", "Settings"], ["Project settings", "/settings/project", "layers", "Settings"],
];

export function CommandMenu({ onClose }: { onClose: () => void }) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const navigate = useNavigate();
  const matches = destinations.filter(([name, , , group]) => `${name} ${group}`.toLowerCase().includes(query.toLowerCase().trim()));
  useEffect(() => { input.current?.focus(); }, []);
  useEffect(() => { document.getElementById(`command-option-${selected}`)?.scrollIntoView?.({ block: "nearest" }); }, [selected]);
  function go(path: string) { navigate(path); onClose(); }
  return <Modal title="Go to…" className="command-menu" onClose={onClose}>
    <div className="command-input"><AppIcon name="search" /><input ref={input} aria-label="Find a page" role="combobox" aria-autocomplete="list" aria-expanded="true" aria-controls="command-results" aria-activedescendant={matches.length ? `command-option-${selected}` : undefined} placeholder="Find a page or setting…" value={query} onChange={(event) => { setQuery(event.target.value); setSelected(0); }} onKeyDown={(event) => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); setSelected((value) => matches.length ? (value + (event.key === "ArrowDown" ? 1 : -1) + matches.length) % matches.length : 0); }
      if (event.key === "Enter" && matches[selected]) { event.preventDefault(); go(matches[selected][1]); }
    }} /><kbd>esc</kbd></div>
    <div id="command-results" role="listbox" aria-label="Pages" className="command-results">{matches.map(([name, path, icon, group], index) => <button key={path} id={`command-option-${index}`} type="button" role="option" aria-selected={index === selected} className={index === selected ? "selected" : ""} onClick={() => go(path)} onMouseMove={() => setSelected(index)}><AppIcon name={icon} /><span>{name}</span><small>{group}</small><AppIcon name="arrow" size={15} /></button>)}{!matches.length && <p className="muted">No pages match “{query}”.</p>}</div>
    <footer><span><kbd>↑</kbd> <kbd>↓</kbd> to navigate</span><span><kbd>↵</kbd> to open</span></footer>
  </Modal>;
}
