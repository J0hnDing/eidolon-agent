import { Link, useParams } from "react-router-dom";
import type { ChangeEvent } from "react";
import { useEffect, useMemo, useState } from "react";

import { SkillRun, Tool, api } from "../api/client";

type ToolFieldType = "text" | "number" | "textarea" | "checkbox" | "select";

type ToolField = {
  name: string;
  label: string;
  type: ToolFieldType;
  placeholder?: string;
  help_text?: string;
  required?: boolean;
  default?: unknown;
  options?: string[];
};

type ToolUiSchema = {
  title?: string;
  description?: string;
  submit_label?: string;
  fields?: ToolField[];
  result_template?: {
    primary_field?: string;
    primary_label?: string;
  };
};

type FormValues = Record<string, string | boolean>;

export default function ToolDetailPage() {
  const { skillId } = useParams();
  const numericSkillId = Number(skillId);
  const [tool, setTool] = useState<Tool | null>(null);
  const [formValues, setFormValues] = useState<FormValues>({});
  const [jsonInput, setJsonInput] = useState("{\n}");
  const [run, setRun] = useState<SkillRun | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isRunning, setIsRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadTool();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [skillId]);

  const uiSchema = useMemo(() => parseToolUiSchema(tool?.skill.tool_ui_schema_json ?? null), [tool]);
  const fields = uiSchema?.fields ?? [];
  const hasForm = fields.length > 0;
  const isReady = tool?.runtime_permission_status === "ready";

  async function loadTool() {
    if (!Number.isInteger(numericSkillId)) {
      setError("Invalid tool id.");
      setIsLoading(false);
      return;
    }
    setIsLoading(true);
    setError(null);
    try {
      const data = await api.getTool(numericSkillId);
      setTool(data);
      const parsedSchema = parseToolUiSchema(data.skill.tool_ui_schema_json);
      const initialValues = buildInitialValues(parsedSchema?.fields ?? []);
      setFormValues(initialValues);
      setJsonInput(JSON.stringify(buildInputFromFields(parsedSchema?.fields ?? [], initialValues), null, 2));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load tool");
    } finally {
      setIsLoading(false);
    }
  }

  async function runTool() {
    if (!tool) return;
    let input: Record<string, unknown>;
    try {
      input = hasForm ? buildInputFromFields(fields, formValues) : (JSON.parse(jsonInput) as Record<string, unknown>);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Input must be valid JSON.");
      return;
    }

    setIsRunning(true);
    setError(null);
    try {
      const response = await api.runTool(tool.skill.id, input);
      setRun(response.run);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not run tool");
    } finally {
      setIsRunning(false);
    }
  }

  if (isLoading) {
    return (
      <section className="page stack">
        <p className="muted">Loading tool...</p>
      </section>
    );
  }

  if (error && !tool) {
    return (
      <section className="page stack">
        <p className="error-text">{error}</p>
        <Link to="/tools">Back to tools</Link>
      </section>
    );
  }

  if (!tool) return null;

  const title = uiSchema?.title || tool.skill.name;
  const description = uiSchema?.description || tool.skill.description;
  const primaryField = uiSchema?.result_template?.primary_field;
  const primaryValue = primaryField && run?.output_json ? run.output_json[primaryField] : undefined;

  return (
    <section className="page stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">Tool</p>
          <h1>{title}</h1>
        </div>
        <Link to="/tools">Back to tools</Link>
      </header>

      <section className="detail-panel stack">
        <p className="muted">{description}</p>
        <dl className="detail-grid">
          <div>
            <dt>Status</dt>
            <dd>{tool.runtime_permission_status}</dd>
          </div>
          <div>
            <dt>Skill Type</dt>
            <dd>{tool.skill.skill_type}</dd>
          </div>
          <div>
            <dt>Risk</dt>
            <dd>{tool.skill.risk_level}</dd>
          </div>
        </dl>
        {!isReady && (
          <p className="error-text">
            {tool.runtime_blocked_reason || "This tool is not available until runtime permissions are ready."}
          </p>
        )}
      </section>

      <section className="form-panel">
        <h2>Input</h2>
        {hasForm ? (
          <div className="tool-form-grid">
            {fields.map((field) => (
              <ToolInput
                key={field.name}
                field={field}
                value={formValues[field.name]}
                onChange={(value) => setFormValues((current) => ({ ...current, [field.name]: value }))}
              />
            ))}
          </div>
        ) : (
          <>
            <label className="field-label" htmlFor="tool-json-input">
              JSON Input
            </label>
            <textarea
              id="tool-json-input"
              className="json-editor"
              value={jsonInput}
              onChange={(event) => setJsonInput(event.target.value)}
              spellCheck={false}
            />
          </>
        )}
        <div className="button-row">
          <button type="button" onClick={runTool} disabled={!isReady || isRunning}>
            {isRunning ? "Running..." : uiSchema?.submit_label || "Run"}
          </button>
          <Link to={`/skills/${tool.skill.id}`}>Open Skill</Link>
        </div>
        {error && <p className="error-text">{error}</p>}
      </section>

      {run && (
        <section className="run-detail detail-panel">
          <h2>Latest Run</h2>
          {primaryValue !== undefined && (
            <div className="tool-result-primary">
              <span>{uiSchema?.result_template?.primary_label || primaryField}</span>
              <strong>{String(primaryValue)}</strong>
            </div>
          )}
          <p>
            <strong>Status:</strong> {run.status}
          </p>
          {run.error_message && <p className="error-text">{run.error_message}</p>}
          {run.output_json && <pre>{JSON.stringify(run.output_json, null, 2)}</pre>}
          {run.stdout && <pre>{run.stdout}</pre>}
          {run.stderr && <pre>{run.stderr}</pre>}
        </section>
      )}
    </section>
  );
}

function ToolInput({
  field,
  value,
  onChange,
}: {
  field: ToolField;
  value: string | boolean | undefined;
  onChange: (value: string | boolean) => void;
}) {
  const id = `tool-field-${field.name}`;
  if (field.type === "checkbox") {
    return (
      <label className="checkbox-row" htmlFor={id}>
        <input
          id={id}
          type="checkbox"
          checked={Boolean(value)}
          onChange={(event) => onChange(event.target.checked)}
        />
        {field.label}
      </label>
    );
  }

  if (field.type === "select") {
    return (
      <label htmlFor={id}>
        {field.label}
        <select id={id} value={String(value ?? "")} onChange={(event) => onChange(event.target.value)}>
          {!field.required && <option value="">None</option>}
          {(field.options ?? []).map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
        {field.help_text && <span className="help-text">{field.help_text}</span>}
      </label>
    );
  }

  const commonProps = {
    id,
    value: String(value ?? ""),
    placeholder: field.placeholder,
    required: field.required,
    onChange: (event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => onChange(event.target.value),
  };

  return (
    <label htmlFor={id}>
      {field.label}
      {field.type === "textarea" ? (
        <textarea {...commonProps} rows={5} />
      ) : (
        <input {...commonProps} type={field.type === "number" ? "number" : "text"} />
      )}
      {field.help_text && <span className="help-text">{field.help_text}</span>}
    </label>
  );
}

function parseToolUiSchema(schema: Record<string, unknown> | null): ToolUiSchema | null {
  if (!schema) return null;
  const fields = Array.isArray(schema.fields)
    ? schema.fields
        .map(parseField)
        .filter((field): field is ToolField => field !== null)
    : [];
  return {
    title: typeof schema.title === "string" ? schema.title : undefined,
    description: typeof schema.description === "string" ? schema.description : undefined,
    submit_label: typeof schema.submit_label === "string" ? schema.submit_label : undefined,
    fields,
    result_template: parseResultTemplate(schema.result_template),
  };
}

function parseField(value: unknown): ToolField | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Record<string, unknown>;
  if (typeof raw.name !== "string" || typeof raw.label !== "string" || typeof raw.type !== "string") {
    return null;
  }
  if (!["text", "number", "textarea", "checkbox", "select"].includes(raw.type)) return null;
  const options = Array.isArray(raw.options)
    ? raw.options.filter((option): option is string => typeof option === "string")
    : undefined;
  return {
    name: raw.name,
    label: raw.label,
    type: raw.type as ToolFieldType,
    placeholder: typeof raw.placeholder === "string" ? raw.placeholder : undefined,
    help_text: typeof raw.help_text === "string" ? raw.help_text : undefined,
    required: typeof raw.required === "boolean" ? raw.required : undefined,
    default: raw.default,
    options,
  };
}

function parseResultTemplate(value: unknown): ToolUiSchema["result_template"] {
  if (!value || typeof value !== "object") return undefined;
  const raw = value as Record<string, unknown>;
  return {
    primary_field: typeof raw.primary_field === "string" ? raw.primary_field : undefined,
    primary_label: typeof raw.primary_label === "string" ? raw.primary_label : undefined,
  };
}

function buildInitialValues(fields: ToolField[]): FormValues {
  return fields.reduce<FormValues>((values, field) => {
    if (field.type === "checkbox") {
      values[field.name] = typeof field.default === "boolean" ? field.default : false;
    } else if (field.default !== undefined) {
      values[field.name] = String(field.default);
    } else if (field.type === "select" && field.required && field.options?.[0]) {
      values[field.name] = field.options[0];
    } else {
      values[field.name] = "";
    }
    return values;
  }, {});
}

function buildInputFromFields(fields: ToolField[], values: FormValues): Record<string, unknown> {
  return fields.reduce<Record<string, unknown>>((input, field) => {
    const value = values[field.name];
    if (field.required && (value === "" || value === undefined)) {
      throw new Error(`${field.label} is required.`);
    }
    if (field.type === "number") {
      if (value === "" || value === undefined) return input;
      const numberValue = Number(value);
      if (Number.isNaN(numberValue)) throw new Error(`${field.label} must be a number.`);
      input[field.name] = numberValue;
    } else {
      input[field.name] = value;
    }
    return input;
  }, {});
}
