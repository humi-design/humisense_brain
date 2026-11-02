/** Overview: real counts and recent synchronization results. No placeholder values. */

import { Link } from "react-router-dom";
import {
  Activity,
  AlertTriangle,
  Boxes,
  CheckCircle2,
  Database,
  Plug,
  PlugZap,
  PowerOff,
  Radio,
} from "lucide-react";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { AsyncBoundary } from "../components/AsyncBoundary";
import {
  Badge,
  Card,
  CardHeading,
  EmptyState,
  StatTile,
  statusTone,
} from "../components/ui";
import { formatDateTime, formatNumber, formatRelative } from "../lib/format";

export function OverviewPage() {
  const state = useAsync((signal) => api.overview(signal), []);

  return (
    <div className="space-y-6 p-6 lg:p-8">
      <header>
        <h1 className="text-xl font-semibold text-ink">Overview</h1>
        <p className="mt-1 text-sm text-ink-muted">
          Connection health and recent synchronization results for this organization.
        </p>
      </header>

      <AsyncBoundary
        loading={state.loading}
        error={state.error}
        data={state.data}
        onRetry={state.reload}
      >
        {(overview) => (
          <>
            <section aria-label="Organization summary">
              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                <StatTile
                  label="Connections"
                  value={formatNumber(overview.connections_total)}
                  icon={Plug}
                  tone="info"
                />
                <StatTile
                  label="Active"
                  value={formatNumber(overview.connections_active)}
                  icon={CheckCircle2}
                  tone="ok"
                />
                <StatTile
                  label="In error"
                  value={formatNumber(overview.connections_error)}
                  icon={AlertTriangle}
                  tone={overview.connections_error > 0 ? "danger" : "neutral"}
                />
                <StatTile
                  label="Records captured"
                  value={formatNumber(overview.records_captured)}
                  icon={Database}
                  tone="accent"
                />
              </div>
            </section>

            <div className="grid gap-6 lg:grid-cols-3">
              <Card className="lg:col-span-2">
                <CardHeading
                  title="Recent synchronization jobs"
                  description="The last ten jobs for this organization."
                  icon={Activity}
                  action={
                    <Link
                      to="/connections"
                      className="text-sm font-medium text-accent hover:underline"
                    >
                      View connections
                    </Link>
                  }
                />
                {overview.recent_jobs.length === 0 ? (
                  <EmptyState
                    icon={PlugZap}
                    title="No synchronization has run yet"
                    description="Add a connection and start a sync to capture records from an authorized source."
                    action={
                      <Link
                        to="/connections/new"
                        className="rounded-lg bg-accent px-3.5 py-2 text-sm font-medium text-canvas hover:bg-accent-strong"
                      >
                        Add a connection
                      </Link>
                    }
                  />
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-left text-sm">
                      <caption className="sr-only">Recent synchronization jobs</caption>
                      <thead className="text-xs uppercase tracking-wide text-ink-faint">
                        <tr className="border-b border-border-subtle">
                          <th scope="col" className="py-2 pr-4 font-medium">Status</th>
                          <th scope="col" className="py-2 pr-4 font-medium">Fetched</th>
                          <th scope="col" className="py-2 pr-4 font-medium">Persisted</th>
                          <th scope="col" className="py-2 pr-4 font-medium">Failed</th>
                          <th scope="col" className="py-2 font-medium">Queued</th>
                        </tr>
                      </thead>
                      <tbody>
                        {overview.recent_jobs.map((job) => (
                          <tr key={job.id} className="border-b border-border-subtle/60 last:border-0">
                            <td className="py-2.5 pr-4">
                              <Badge tone={statusTone(job.status)}>{job.status}</Badge>
                              {job.error_code ? (
                                <span className="ml-2 font-mono text-xs text-danger">
                                  {job.error_code}
                                </span>
                              ) : null}
                            </td>
                            <td className="py-2.5 pr-4 tabular-nums text-ink-muted">
                              {formatNumber(job.records_fetched)}
                            </td>
                            <td className="py-2.5 pr-4 tabular-nums text-ink-muted">
                              {formatNumber(job.records_persisted)}
                            </td>
                            <td className="py-2.5 pr-4 tabular-nums text-ink-muted">
                              {formatNumber(job.records_failed)}
                            </td>
                            <td className="py-2.5 text-ink-muted" title={formatDateTime(job.queued_at)}>
                              {formatRelative(job.queued_at)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Card>

              <Card>
                <CardHeading
                  title="Platform status"
                  description="Registry and handoff state."
                  icon={Boxes}
                />
                <dl className="space-y-4 text-sm">
                  <div className="flex items-center justify-between gap-3">
                    <dt className="text-ink-muted">Connector types available</dt>
                    <dd className="font-medium tabular-nums text-ink">
                      {formatNumber(overview.connector_types_available)}
                    </dd>
                  </div>
                  <div className="flex items-center justify-between gap-3">
                    <dt className="text-ink-muted">Not yet configured</dt>
                    <dd className="font-medium tabular-nums text-ink">
                      {formatNumber(overview.connector_types_not_configured)}
                    </dd>
                  </div>
                  <div className="flex items-center justify-between gap-3">
                    <dt className="text-ink-muted">Disabled connections</dt>
                    <dd className="font-medium tabular-nums text-ink">
                      {formatNumber(overview.connections_disabled)}
                    </dd>
                  </div>
                  <div className="flex items-center justify-between gap-3">
                    <dt className="text-ink-muted">Ingestion sink</dt>
                    <dd>
                      <Badge tone={overview.layer2_configured ? "ok" : "warn"}>
                        <Radio size={12} aria-hidden="true" />
                        {overview.ingestion_sink}
                      </Badge>
                    </dd>
                  </div>
                  <div className="flex items-center justify-between gap-3">
                    <dt className="text-ink-muted">Layer 2 handoff</dt>
                    <dd>
                      <Badge tone={overview.layer2_configured ? "ok" : "warn"}>
                        {overview.layer2_configured ? "configured" : "not configured"}
                      </Badge>
                    </dd>
                  </div>
                  {!overview.layer2_configured ? (
                    <p className="flex items-start gap-2 rounded-lg border border-warn/30 bg-warn/10 p-3 text-xs text-warn">
                      <PowerOff size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
                      Records are captured and persisted, but not yet forwarded to Layer 2.
                    </p>
                  ) : null}
                </dl>
              </Card>
            </div>
          </>
        )}
      </AsyncBoundary>
    </div>
  );
}
