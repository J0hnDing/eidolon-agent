import { Link } from "react-router-dom";

import githubIcon from "../../../resource/integrations/github.svg";
import googleIcon from "../../../resource/integrations/google.svg";
import notionIcon from "../../../resource/integrations/notion.svg";
import telegramIcon from "../../../resource/integrations/telegram.svg";
import type { IntegrationStatuses } from "./useIntegrationStatuses";

export const integrations = [
  { id: "google", title: "Google", description: "Calendar, Gmail, and Drive", group: "Connected apps", icon: googleIcon },
  { id: "github", title: "GitHub", description: "Repositories and account access", group: "Connected apps", icon: githubIcon },
  { id: "microsoft", title: "Microsoft Outlook", description: "Mail and Microsoft account access", group: "Connected apps", icon: null },
  { id: "notion", title: "Notion", description: "Todos, reports, and Daily Feed", group: "Connected apps", icon: notionIcon },
  { id: "quercus", title: "Quercus", description: "Course selection and knowledge sync", group: "Connected apps", icon: null },
  { id: "telegram", title: "Telegram", description: "Notifications, approvals, and agent bots", group: "Messaging", icon: telegramIcon },
  { id: "wecom", title: "WeCom", description: "Observer bot and paired users", group: "Messaging", icon: null },
  { id: "atlas", title: "Eidolon-Atlas", description: "Local companion and vault access", group: "Local tools & data", icon: null },
  { id: "codex", title: "Codex tools", description: "Eidolon tools in Codex sessions", group: "Local tools & data", icon: null },
  { id: "browser-authentication", title: "Browser authentication", description: "U of T Weblogin credentials", group: "Local tools & data", icon: null },
  { id: "macro-data", title: "Macro data", description: "FRED, BLS, BEA, and EIA API keys", group: "Local tools & data", icon: null },
] as const;

export type IntegrationId = typeof integrations[number]["id"];

export function IntegrationIcon({ id }: { id: IntegrationId }) {
  const integration = integrations.find((item) => item.id === id)!;
  if (integration.icon) {
    return <span className={`integration-icon integration-icon-${id}`} aria-hidden="true"><img src={integration.icon} alt="" /></span>;
  }

  return (
    <span className={`integration-icon integration-icon-${id}`} aria-hidden="true">
      {id === "microsoft" ? (
        <svg viewBox="0 0 24 24"><path fill="#f25022" d="M1 1h10v10H1z" /><path fill="#7fba00" d="M13 1h10v10H13z" /><path fill="#00a4ef" d="M1 13h10v10H1z" /><path fill="#ffb900" d="M13 13h10v10H13z" /></svg>
      ) : (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
          {id === "quercus" && <><path d="M4 4h7a3 3 0 0 1 3 3v13a4 4 0 0 0-4-2H4V4Zm10 3a3 3 0 0 1 3-3h3v14h-3a4 4 0 0 0-3 2" /></>}
          {id === "atlas" && <><circle cx="12" cy="12" r="9" /><ellipse cx="12" cy="12" rx="4" ry="9" /><path d="M3 12h18M5 6.5h14M5 17.5h14" /></>}
          {id === "browser-authentication" && <><rect x="4" y="10" width="16" height="11" rx="2" /><path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v3" /></>}
          {id === "macro-data" && <><path d="M4 3v17h17M8 16v-5M13 16V6M18 16V9" /></>}
          {id === "codex" && <path d="m7 6-5 6 5 6m10-12 5 6-5 6M14 4l-4 16" />}
          {id === "wecom" && <><path d="M4 4h16v12H9l-5 4V4Z" /><path d="M8 10h.01M12 10h.01M16 10h.01" strokeWidth="3" /></>}
        </svg>
      )}
    </span>
  );
}

export function IntegrationsDirectory({ statuses }: { statuses: IntegrationStatuses }) {
  return (
    <div className="integrations-directory">
      {["Connected apps", "Messaging", "Local tools & data"].map((group) => (
        <section className="integration-group" key={group} aria-label={group}>
          <h2>{group}</h2>
          <div className="integration-grid">
            {integrations.filter((item) => item.group === group).map((item) => (
              <Link className="integration-tile" to={`/settings/integrations/${item.id}`} key={item.id} aria-label={item.title} aria-describedby={`${item.id}-connection-status`}>
                <IntegrationIcon id={item.id} />
                <span className="integration-tile-copy">
                  <strong>{item.title}</strong>
                  <span>{item.description}</span>
                  <span id={`${item.id}-connection-status`} className={`integration-connection-state ${statuses[item.id]?.state ?? "checking"}`} title={statuses[item.id]?.description}>
                    <span className="status-dot" aria-hidden="true" />
                    {statuses[item.id] ? { connected: "Connected", partial: "Partial", disconnected: "Disconnected", unavailable: "Unavailable" }[statuses[item.id]!.state] : "Checking…"}
                    <span className="integration-status-description">{statuses[item.id] ? `. ${statuses[item.id]!.description}` : ""}</span>
                  </span>
                </span>
                <svg className="integration-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><path d="m9 5 7 7-7 7" /></svg>
              </Link>
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
