import { FunctionCatalog } from "./FunctionsPage";
import { SettingsLayout } from "./UsageSettingsPage";

export default function FunctionsSettingsPage() {
  return (
    <SettingsLayout wide>
      <FunctionCatalog />
    </SettingsLayout>
  );
}
