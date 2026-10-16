import { Navigate, useLocation, useParams } from "react-router-dom";

import { integrations } from "../features/integrations/IntegrationNavigation";
import UsageSettingsPage from "./UsageSettingsPage";

export default function IntegrationsPage() {
  const { integrationId } = useParams();
  const { search } = useLocation();
  const integration = integrations.find((item) => item.id === integrationId);

  // Existing backend OAuth callbacks return to the overview URL.
  if (!integrationId) {
    const result = new URLSearchParams(search);
    const provider = result.has("outlook") ? "microsoft"
      : ["google_calendar", "gmail", "google_drive"].some((key) => result.has(key)) ? "google" : null;
    if (provider) return <Navigate to={`/settings/integrations/${provider}${search}`} replace />;
  } else if (!integration) {
    return <Navigate to="/settings/integrations" replace />;
  }

  return <UsageSettingsPage key={integration?.id ?? "overview"} section="integrations" integration={integration?.id} />;
}
