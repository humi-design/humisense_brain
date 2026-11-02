/**
 * Add-connection wizard.
 *
 * The form is generated from the connector manifest's `config_fields`, so it can only
 * collect fields the connector actually declares. Three deliberate properties:
 *
 * - Secret fields are collected into a separate `credentials` object, which the backend
 *   routes to the secrets provider. They are never echoed back or written to the config.
 * - The review step shows the non-secret configuration that will be saved, so the operator
 *   confirms what is sent rather than a summary the UI invented.
 * - "Test" is an honest probe. A failure is reported as a failure; it never blocks saving,
 *   because a source may simply be unreachable at that moment.
 */

import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { ArrowLeft, ArrowRight, Check, FlaskConical, Save } from "lucide-react";
import { api, ApiError } from "../api/client";
import type { ConfigField, Connector, ConnectionTestResult } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { Alert, Badge, Button, Card, CardHeading, Field, Spinner, inputClasses } from "../components/ui";
import { renderValue } from "../lib/format";

type Step = "connector" | "configure" | "review";
type ConfigValues = Record<string, string | boolean>;

function initialValues(connector: Connector): ConfigValues {
  const values: ConfigValues = {};
  for (const field of connector.config_fields) {
    if (field.field_type === "boolean") {
      values[field.name] = field.default === true;
    } else if (typeof field.default === "string" || typeof field.default === "number") {
      values[field.name] = String(field.default);
    } else {
      values[field.name] = "";
    }
  }
  return values;
}

function coerce(field: ConfigField, raw: string | boolean): unknown {
  if (field.field_type === "boolean") {
    return raw === true;
  }
  const text = String(raw);
  if (text === "") {
    return undefined;
  }
  if (field.field_type === "integer") {
    const parsed = Number.parseInt(text, 10);
    return Number.isNaN(parsed) ? text : parsed;
  }
  return text;
}

function ConfigInput({
  field,
  value,
  onChange,
}: {
  field: ConfigField;
  value: string | boolean;
  onChange: (next: string | boolean) => void;
}) {
  const id = `config-${field.name}`;

  if (field.field_type === "boolean") {
    return (
      <div className="flex items-start gap-3">
        <input
          id={id}
          type="checkbox"
          checked={value === true}
          onChange={(event) => onChange(event.target.checked)}
          className="mt-0.5 h-4 w-4 rounded border-border-subtle bg-canvas accent-[var(--color-accent)]"
        />
        <div>
          <label htmlFor={id} className="text-sm font-medium text-ink">
            {field.label}
          </label>
          {field.help_text ? (
            <p className="text-xs text-ink-faint">{field.help_text}</p>
          ) : null}
        </div>
      </div>
    );
  }

  if (field.field_type === "enum" && field.choices.length > 0) {
    return (
      <Field label={field.label} htmlFor={id} required={field.required} hint={field.help_text}>
        <select
          id={id}
          className={inputClasses}
          value={String(value)}
          onChange={(event) => onChange(event.target.value)}
        >
          <option value="">Select…</option>
          {field.choices.map((choice) => (
            <option key={choice} value={choice}>
              {choice}
            </option>
          ))}
        </select>
      </Field>
    );
  }

  return (
    <Field
      label={field.label}
      htmlFor={id}
      required={field.required}
      hint={field.help_text}
    >
      <input
        id={id}
        type={field.field_type === "integer" ? "number" : "text"}
        className={`${inputClasses} ${field.field_type === "path" ? "font-mono text-xs" : ""}`}
        value={String(value)}
        placeholder={field.placeholder ?? ""}
        maxLength={field.max_length ?? undefined}
        onChange={(event) => onChange(event.target.value)}
        aria-describedby={field.help_text ? `${id}-hint` : undefined}
      />
    </Field>
  );
}

export function AddConnectionPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const preselect = searchParams.get("connector");

  const catalog = useAsync((signal) => api.connectors(signal), []);
  const [step, setStep] = useState<Step>("connector");
  const [chosen, setChosen] = useState<Connector | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [values, setValues] = useState<ConfigValues>({});
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [testResult, setTestResult] = useState<ConnectionTestResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  useEffect(() => {
    if (!preselect || chosen || !catalog.data) {
      return;
    }
    const match = catalog.data.find(
      (connector) =>
        connector.connector_type === preselect && connector.availability === "available",
    );
    if (match) {
      setChosen(match);
      setValues(initialValues(match));
      setStep("configure");
    }
  }, [preselect, catalog.data, chosen]);

  const configFields = useMemo(
    () => chosen?.config_fields.filter((field) => !field.is_secret) ?? [],
    [chosen],
  );
  const secretFields = useMemo(
    () => chosen?.config_fields.filter((field) => field.is_secret) ?? [],
    [chosen],
  );

  const missingRequired = configFields.filter((field) => {
    if (!field.required) {
      return false;
    }
    const value = values[field.name];
    return value === undefined || value === "" || value === false && field.field_type !== "boolean";
  });

  function buildConfig(): Record<string, unknown> {
    const config: Record<string, unknown> = {};
    for (const field of configFields) {
      const coerced = coerce(field, values[field.name] ?? "");
      if (coerced !== undefined) {
        config[field.name] = coerced;
      }
    }
    return config;
  }

  function buildCredentials(): Record<string, unknown> {
    const credentials: Record<string, unknown> = {};
    for (const field of secretFields) {
      const value = secrets[field.name];
      if (value) {
        credentials[field.name] = value;
      }
    }
    return credentials;
  }

  function onSubmit(): void {
    void (async () => {
    if (!chosen) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const created = await api.createConnection({
        connector_type: chosen.connector_type,
        name: name.trim(),
        description: description.trim() ? description.trim() : null,
        config: buildConfig(),
        credentials: buildCredentials(),
      });
      void navigate(`/connections/${created.id}`, { replace: true });
    } catch (cause) {
      setError(
        cause instanceof ApiError
          ? cause
          : new ApiError(0, "unknown", "The connection could not be created.", null, null),
      );
    } finally {
      setBusy(false);
    }
    })();
  }

  return (
    <div className="mx-auto max-w-3xl space-y-6 p-6 lg:p-8">
      <header>
        <Link
          to="/connections"
          className="inline-flex items-center gap-1.5 text-sm text-ink-muted hover:text-ink"
        >
          <ArrowLeft size={14} aria-hidden="true" />
          Back to connections
        </Link>
        <h1 className="mt-3 text-xl font-semibold text-ink">Add a connection</h1>
        <p className="mt-1 text-sm text-ink-muted">
          Configure one authorized source for your organization.
        </p>
      </header>

      <ol className="flex items-center gap-3 text-sm" aria-label="Wizard progress">
        {(["connector", "configure", "review"] as Step[]).map((item, index) => {
          const active = item === step;
          const done = ["connector", "configure", "review"].indexOf(step) > index;
          return (
            <li key={item} className="flex items-center gap-2">
              <span
                aria-current={active ? "step" : undefined}
                className={`flex h-6 w-6 items-center justify-center rounded-full text-xs font-semibold ${
                  done
                    ? "bg-ok/20 text-ok"
                    : active
                      ? "bg-accent/20 text-accent"
                      : "bg-surface-raised text-ink-faint"
                }`}
              >
                {done ? <Check size={13} aria-hidden="true" /> : index + 1}
              </span>
              <span className={active ? "font-medium text-ink" : "text-ink-muted"}>
                {item === "connector" ? "Connector" : item === "configure" ? "Configure" : "Review"}
              </span>
              {index < 2 ? <span className="text-ink-faint" aria-hidden="true">›</span> : null}
            </li>
          );
        })}
      </ol>

      {error ? <Alert tone="error" title="The request failed">{error.message}</Alert> : null}

      {step === "connector" ? (
        <Card>
          <CardHeading title="Choose a connector type" description="Only available connectors can be configured." />
          {catalog.loading ? (
            <Spinner />
          ) : (
            <div className="space-y-2">
              {(catalog.data ?? [])
                .filter((connector) => connector.availability === "available")
                .map((connector) => (
                  <button
                    key={connector.connector_type}
                    type="button"
                    onClick={() => {
                      setChosen(connector);
                      setValues(initialValues(connector));
                      setTestResult(null);
                      setStep("configure");
                    }}
                    className="w-full rounded-lg border border-border-subtle bg-surface-raised p-4 text-left transition-colors hover:border-accent/60"
                  >
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-ink">{connector.display_name}</span>
                      <Badge tone="ok">available</Badge>
                      {connector.synthetic_only ? <Badge tone="info">synthetic only</Badge> : null}
                    </div>
                    <p className="mt-1 text-sm text-ink-muted">{connector.description}</p>
                  </button>
                ))}
            </div>
          )}
        </Card>
      ) : null}

      {step === "configure" && chosen ? (
        <>
          <Card>
            <CardHeading title="Connection details" />
            <div className="space-y-4">
              <Field label="Name" htmlFor="connection-name" required hint="A label for your team.">
                <input
                  id="connection-name"
                  className={inputClasses}
                  value={name}
                  maxLength={120}
                  onChange={(event) => setName(event.target.value)}
                />
              </Field>
              <Field label="Description" htmlFor="connection-description" hint="Optional.">
                <input
                  id="connection-description"
                  className={inputClasses}
                  value={description}
                  maxLength={500}
                  onChange={(event) => setDescription(event.target.value)}
                />
              </Field>
            </div>
          </Card>

          <Card>
            <CardHeading
              title="Source configuration"
              description="These fields come from the connector's own manifest."
            />
            <div className="space-y-4">
              {configFields.map((field) => (
                <ConfigInput
                  key={field.name}
                  field={field}
                  value={values[field.name] ?? ""}
                  onChange={(next) => setValues((current) => ({ ...current, [field.name]: next }))}
                />
              ))}
            </div>
          </Card>

          {secretFields.length > 0 ? (
            <Card>
              <CardHeading
                title="Credentials"
                description="Stored in the secrets provider, never returned after saving."
              />
              <div className="space-y-4">
                {secretFields.map((field) => (
                  <Field
                    key={field.name}
                    label={field.label}
                    htmlFor={`secret-${field.name}`}
                    required={field.required}
                    hint={field.help_text || "Encrypted at rest. Never shown again."}
                  >
                    <input
                      id={`secret-${field.name}`}
                      type="password"
                      className={inputClasses}
                      value={secrets[field.name] ?? ""}
                      autoComplete="new-password"
                      onChange={(event) =>
                        setSecrets((current) => ({ ...current, [field.name]: event.target.value }))
                      }
                    />
                  </Field>
                ))}
              </div>
            </Card>
          ) : null}

          <div className="flex justify-between gap-3">
            <Button onClick={() => setStep("connector")} icon={ArrowLeft}>
              Back
            </Button>
            <Button
              variant="primary"
              icon={ArrowRight}
              disabled={!name.trim() || missingRequired.length > 0}
              onClick={() => setStep("review")}
            >
              Review
            </Button>
          </div>
          {missingRequired.length > 0 ? (
            <p className="text-xs text-warn">
              Required fields still to fill: {missingRequired.map((field) => field.label).join(", ")}
            </p>
          ) : null}
        </>
      ) : null}

      {step === "review" && chosen ? (
        <>
          <Card>
            <CardHeading
              title="Review and confirm"
              description="Confirm exactly what will be saved."
            />
            <dl className="space-y-3 text-sm">
              <div className="flex justify-between gap-4">
                <dt className="text-ink-muted">Connector type</dt>
                <dd className="font-mono text-xs text-ink">{chosen.connector_type}</dd>
              </div>
              <div className="flex justify-between gap-4">
                <dt className="text-ink-muted">Name</dt>
                <dd className="text-ink">{name || "—"}</dd>
              </div>
              {configFields.map((field) => {
                const value = coerce(field, values[field.name] ?? "");
                if (value === undefined) {
                  return null;
                }
                return (
                  <div key={field.name} className="flex justify-between gap-4">
                    <dt className="text-ink-muted">{field.label}</dt>
                    <dd className="break-all text-right text-ink">
                      {renderValue(value)}
                    </dd>
                  </div>
                );
              })}
              {secretFields.length > 0 ? (
                <div className="flex justify-between gap-4">
                  <dt className="text-ink-muted">Credentials</dt>
                  <dd className="text-ink">
                    {secretFields.filter((field) => secrets[field.name]).length} of{" "}
                    {secretFields.length} provided
                  </dd>
                </div>
              ) : null}
            </dl>
          </Card>

          <Card>
            <CardHeading
              title="Test the connection"
              description="Optional. A probe against the source; it never changes source data."
            />
            <div className="space-y-3">
              <Button
                icon={FlaskConical}
                loading={busy}
                disabled={!name.trim()}
                onClick={() => {
                  void (async () => {
                  setBusy(true);
                  setError(null);
                  setTestResult(null);
                  try {
                    // A connection must exist to test it, so create then probe. If the probe
                    // fails the connection still exists and is reported as unhealthy.
                    const created = await api.createConnection({
                      connector_type: chosen.connector_type,
                      name: name.trim(),
                      description: description.trim() ? description.trim() : null,
                      config: buildConfig(),
                      credentials: buildCredentials(),
                    });
                    const result = await api.testConnection(created.id);
                    setTestResult(result);
                  } catch (cause) {
                    setError(
                      cause instanceof ApiError
                        ? cause
                        : new ApiError(0, "unknown", "The test could not be completed.", null, null),
                    );
                  } finally {
                    setBusy(false);
                  }
                  })();
                }}
              >
                Create and test
              </Button>

              {testResult ? (
                <Alert
                  tone={testResult.ok ? "success" : "error"}
                  title={testResult.ok ? "Connection succeeded" : "Connection failed"}
                >
                  <p>{testResult.message}</p>
                  {testResult.error_code ? (
                    <p className="mt-1 font-mono text-xs">{testResult.error_code}</p>
                  ) : null}
                  {testResult.latency_ms !== null ? (
                    <p className="mt-1 text-xs opacity-80">
                      {Math.round(testResult.latency_ms)} ms
                    </p>
                  ) : null}
                </Alert>
              ) : null}

              {testResult ? (
                <Button
                  variant="primary"
                  onClick={() => {
                    void navigate("/connections", { replace: true });
                  }}
                >
                  Done
                </Button>
              ) : null}
            </div>
          </Card>

          {!testResult ? (
            <div className="flex justify-between gap-3">
              <Button onClick={() => setStep("configure")} icon={ArrowLeft}>
                Back
              </Button>
              <Button
                variant="primary"
                icon={Save}
                loading={busy}
                disabled={!name.trim()}
                onClick={onSubmit}
              >
                Save connection
              </Button>
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
