/**
 * Connector catalog. Rendered entirely from the registry the backend reports, so the
 * capabilities shown are the capabilities that exist — nothing is advertised by hand.
 */

import { Link } from "react-router-dom";
import { Boxes, ExternalLink, Info, Plus, Wrench } from "lucide-react";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { AsyncBoundary } from "../components/AsyncBoundary";
import { Alert, Badge, Card, EmptyState, Spinner } from "../components/ui";

const CAPABILITY_LABELS: Record<string, string> = {
  supports_read: "Read",
  supports_write: "Write",
  supports_incremental: "Incremental sync",
  supports_pagination: "Pagination",
  supports_timestamps: "Source timestamps",
  supports_metadata: "Metadata",
  supports_search: "Search",
  supports_webhook: "Webhook / event subscription",
  supports_live: "Live connectivity",
  supports_health_check: "Health check",
};

export function CatalogPage() {
  const state = useAsync((signal) => api.connectors(signal), []);

  return (
    <div className="space-y-6 p-6 lg:p-8">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-ink">Connector catalog</h1>
          <p className="mt-1 text-sm text-ink-muted">
            Integration types available to this deployment. Capabilities are reported by the
            registry and reflect what is actually implemented.
          </p>
        </div>
        <Link
          to="/connections/new"
          className="inline-flex items-center gap-2 rounded-lg bg-accent px-3.5 py-2 text-sm font-medium text-canvas hover:bg-accent-strong"
        >
          <Plus size={16} aria-hidden="true" />
          Add connection
        </Link>
      </header>

      <AsyncBoundary
        loading={state.loading}
        error={state.error}
        data={state.data}
        onRetry={state.reload}
        empty={(connectors) =>
          connectors.length === 0 ? (
            <EmptyState
              icon={Boxes}
              title="No connectors are registered"
              description="The connector registry is empty. This is a deployment problem, not a configuration one."
            />
          ) : null
        }
      >
        {(connectors) => (
          <div className="grid gap-4 lg:grid-cols-2">
            {connectors.map((connector) => {
              const available = connector.availability === "available";
              const enabledCapabilities = Object.entries(connector.capabilities).filter(
                ([, enabled]) => enabled,
              );
              return (
                <Card key={connector.connector_type} as="article">
                  <div className="flex items-start justify-between gap-4">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <h2 className="text-base font-semibold text-ink">
                          {connector.display_name}
                        </h2>
                        <Badge tone={available ? "ok" : "warn"}>{connector.availability}</Badge>
                        {connector.synthetic_only ? (
                          <Badge tone="info">synthetic data only</Badge>
                        ) : null}
                      </div>
                      <p className="mt-1 font-mono text-xs text-ink-faint">
                        {connector.connector_type} · v{connector.version} · {connector.vendor}
                      </p>
                    </div>
                  </div>

                  <p className="mt-3 text-sm text-ink-muted">{connector.description}</p>

                  {!available && connector.unavailable_reason ? (
                    <div className="mt-3">
                      <Alert tone="warning" title="Not configured">
                        {connector.unavailable_reason}
                      </Alert>
                    </div>
                  ) : null}

                  <div className="mt-4">
                    <p className="text-xs font-medium uppercase tracking-wide text-ink-faint">
                      Capabilities
                    </p>
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {enabledCapabilities.length === 0 ? (
                        <span className="text-xs text-ink-faint">
                          None declared. This connector is read-only by omission.
                        </span>
                      ) : (
                        enabledCapabilities.map(([key]) => (
                          <Badge key={key} tone="accent">
                            {CAPABILITY_LABELS[key] ?? key}
                          </Badge>
                        ))
                      )}
                    </div>
                  </div>

                  <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2">
                    <div>
                      <dt className="text-xs uppercase tracking-wide text-ink-faint">
                        Authentication
                      </dt>
                      <dd className="mt-0.5 text-ink-muted">
                        {connector.auth_modes.length > 0
                          ? connector.auth_modes.join(", ")
                          : "none required"}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-xs uppercase tracking-wide text-ink-faint">
                        Record types
                      </dt>
                      <dd className="mt-0.5 text-ink-muted">
                        {connector.record_types.length > 0
                          ? connector.record_types.join(", ")
                          : "—"}
                      </dd>
                    </div>
                  </dl>

                  {connector.required_setup.length > 0 ? (
                    <div className="mt-4 rounded-lg border border-border-subtle bg-canvas p-3">
                      <p className="flex items-center gap-2 text-xs font-medium text-ink-muted">
                        <Wrench size={13} aria-hidden="true" />
                        Required setup
                      </p>
                      <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-ink-muted">
                        {connector.required_setup.map((item) => (
                          <li key={item}>{item}</li>
                        ))}
                      </ul>
                    </div>
                  ) : null}

                  <div className="mt-4 flex items-center justify-between gap-3">
                    {connector.documentation_url ? (
                      <a
                        href={connector.documentation_url}
                        target="_blank"
                        rel="noreferrer noopener"
                        className="inline-flex items-center gap-1.5 text-sm text-accent hover:underline"
                      >
                        <ExternalLink size={14} aria-hidden="true" />
                        Documentation
                      </a>
                    ) : (
                      <span />
                    )}
                    {available ? (
                      <Link
                        to={`/connections/new?connector=${encodeURIComponent(connector.connector_type)}`}
                        className="inline-flex items-center gap-1.5 text-sm font-medium text-accent hover:underline"
                      >
                        <Plus size={14} aria-hidden="true" />
                        Configure
                      </Link>
                    ) : (
                      <span className="inline-flex items-center gap-1.5 text-sm text-ink-faint">
                        <Info size={14} aria-hidden="true" />
                        Unavailable
                      </span>
                    )}
                  </div>
                </Card>
              );
            })}
          </div>
        )}
      </AsyncBoundary>
      {state.loading && state.data ? <Spinner label="Refreshing…" /> : null}
    </div>
  );
}
