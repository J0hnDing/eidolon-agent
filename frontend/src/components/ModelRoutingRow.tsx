import { CodexInvocationChoice, CodexModelCatalog } from "../api/client";
import "./model-routing.css";

export function RoutingRow({
  label,
  choice,
  fallback,
  models,
  onChange,
}: {
  label: string;
  choice: CodexInvocationChoice;
  fallback?: CodexInvocationChoice;
  models: CodexModelCatalog["models"];
  onChange: (choice: CodexInvocationChoice) => void;
}) {
  const defaultModel = models.find((model) => model.is_default) ?? models[0];
  const selectedModelName = choice.model ?? fallback?.model ?? defaultModel?.model ?? null;
  const selectedModel = models.find((model) => model.model === selectedModelName || model.id === selectedModelName);
  const efforts = selectedModel?.supported_reasoning_efforts ?? [];
  return (
    <div className="settings-routing-row">
      <div className="settings-routing-label">
        <strong>{label}</strong>
        <span>{fallback ? "Inherits the role default unless changed." : "Choose a model and its reasoning effort."}</span>
      </div>
      <label>
        Model
        <select
          aria-label={`${label} model`}
          value={choice.model ?? ""}
          onChange={(event) => onChange({ model: event.target.value || null, reasoning_effort: null })}
        >
          <option value="">{fallback ? "Inherit role default" : "Codex default"}</option>
          {models.map((model) => (
            <option key={model.id} value={model.model}>{model.display_name} ({model.model})</option>
          ))}
        </select>
      </label>
      <label>
        Reasoning effort
        <select
          aria-label={`${label} effort`}
          value={choice.reasoning_effort ?? ""}
          onChange={(event) => onChange({ ...choice, reasoning_effort: event.target.value || null })}
        >
          <option value="">{fallback ? "Inherit role/model default" : "Model default"}</option>
          {efforts.map((effort) => <option key={effort} value={effort}>{effort}</option>)}
        </select>
      </label>
    </div>
  );
}

