/**
 * Connection detail.
 *
 * Shows capabilities, health, sync history, an authorized record preview, redacted
 * configuration, and audit history. Record payloads are rendered as text with React's
 * escaping — connected data is untrusted and is never interpreted as markup.
 */

import { useCallback, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  ArrowLeft,
  Ban,
  Database,
  FlaskConical,
  Info,
  Play,
  RefreshCw,
  ShieldAlert,
  Trash2,
} from "lucide-react";
import { api, ApiError } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { useSession } from "../hooks/useSession";
import { AsyncBoundary } from "../components/AsyncBoundary";
import {
  Alert,
  Badge,
  Button,
  Card,
  CardHeading,
  DefinitionList,
  EmptyState,
  Spinner,
  statusTone,
} from "../components/ui";
import {
  formatDateTime,
  formatDuration,
  formatNumber,
  formatRelative,
  renderValue,
  truncate,
} from "../lib/format";

export function ConnectionDetailPage() {
  const { connectionId = "" } = useParams();
  const { can } = useSession();
  const [actionError, setActionError] = useState<ApiError | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);

  const connection = useAsync(
    (signal) => api.connection(connectionId, signal),
    [connectionId],
  );
  const status = useAsync(
    (signal) => api.connectionStatus(connectionId, signal),
    [connectionId],
  );
  const jobs = useAsync(
    (signal) => api.connectionJobs(connectionId, { limit: 20 }, signal),
    [connectionId],
  );
  const records = useAsync(
    (signal) => api.connectionRecords(connectionId, { limit: 20 }, signal),
    [connectionId],
  );

  const reloadAll = useCallback(() => {
    connection.reload();
    status.reload();
    jobs.reload();
    records.reload();
  }, [connection, status, jobs, records]);

  async function runAction(
    label: string,
    action: () => Promise<unknown>,
    reload = true,
  ) {
    setBusyAction(label);
    setActionError(null);
    try {
      await action();
      if (reload) {
        reloadAll();
      }
    } catch (cause) {
      setActionError(
        cause instanceof ApiError
          ? cause
          : new ApiError(0, "unknown", "The action could not be completed.", null, null),
      );
    } finally {
      setBusyAction(null);
    }
  }

  return (
    <div className="space-y-6 p-6 lg:p-8">
      <header>
        <Link
          to="/connections"
          className="inline-flex items-center gap-1.5 text-sm text-ink-muted hover:text-ink"
        >
          <ArrowLeft size={14} aria-hidden="true" />
          Back to connections
        </Link>
        <AsyncBoundary
          loading={connection.loading}
          error={connection.error}
          data={connection.data}
          onRetry={connection.reload}
        >
          {(item) => (
            <div className="mt-3 flex flex-wrap items-start justify-between gap-4">
              <div>
                <div className="flex flex-wrap items-center gap-2">
                  <h1 className="text-xl font-semibold text-ink">{item.name}</h1>
                  <Badge tone={statusTone(item.status)}>{item.status}</Badge>
                  <Badge tone={item.enabled ? "ok" : "warn"}>
                    {item.enabled ? "enabled" : "disabled"}
                  </Badge>
                </div>
                <p className="mt-1 font-mono text-xs text-ink-faint">
                  {item.connector_type} · {item.id}
                </p>
                {item.description ? (
                  <p className="mt-2 max-w-2xl text-sm text-ink-muted">{item.description}</p>
                ) : null}
              </div>

              <div className="flex flex-wrap gap-2">
                {can("connections:test") ? (
                  <Button
                    icon={FlaskConical}
                    loading={busyAction === "test"}
                    onClick={() => {
                      void runAction("test", () => api.testConnection(connectionId));
                    }}
                  >
                    Test
                  </Button>
                ) : null}
                {can("sync:run") ? (
                  <Button
                    variant="primary"
                    icon={Play}
                    loading={busyAction === "sync"}
                    onClick={() => {
                      void runAction("sync", () => api.startSync(connectionId));
                    }}
                  >
                    Start sync
                  </Button>
                ) : null}
                <Button icon={RefreshCw} onClick={reloadAll} loading={busyAction === "refresh"}>
                  Refresh
                </Button>
              </div>
            </div>
          )}
        </AsyncBoundary>
      </header>

      {actionError ? (
        <Alert tone="error" title="The action failed">
          <p>{actionError.message}</p>
          {actionError.correlationId ? (
            <p className="mt-1 font-mono text-xs opacity-80">
              correlation id: {actionError.correlationId}
            </p>
          ) : null}
        </Alert>
      ) : null}

      <div className="grid gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeading title="Health and capabilities" icon={Database} />
          <AsyncBoundary
            loading={status.loading}
            error={status.error}
            data={status.data}
            onRetry={status.reload}
          >
            {(item) => (
              <>
                <DefinitionList
                  items={[
                    { term: "Records captured", detail: formatNumber(item.record_count) },
                    { term: "Last tested", detail: formatRelative(item.last_tested_at) },
                    { term: "Last success", detail: formatRelative(item.last_success_at) },
                    {
                      term: "Last error",
                      detail: item.last_error_code ? (
                        <span className="font-mono text-xs text-danger">{item.last_error_code}</span>
                      ) : (
                        "none"
                      ),
                    },
                  ]}
                />
                <div className="mt-5">
                  <p className="text-xs font-medium uppercase tracking-wide text-ink-faint">
                    Declared capabilities
                  </p>
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {Object.entries(item.connector_capabilities)
                      .filter(([, enabled]) => enabled)
                      .map(([key]) => (
                        <Badge key={key} tone="accent">
                          {key.replace(/^supports_/, "").replace(/_/g, " ")}
                        </Badge>
                      ))}
                  </div>
                </div>
              </>
            )}
          </AsyncBoundary>
        </Card>

        <Card>
          <CardHeading title="Configuration" description="Secret values are never returned." />
          <AsyncBoundary
            loading={connection.loading}
            error={connection.error}
            data={connection.data}
            onRetry={connection.reload}
          >
            {(item) => (
              <dl className="space-y-2 text-sm">
                {Object.entries(item.config).length === 0 ? (
                  <p className="text-ink-faint">No non-secret configuration.</p>
                ) : (
                  Object.entries(item.config).map(([key, value]) => (
                    <div key={key} className="flex justify-between gap-3">
                      <dt className="text-ink-muted">{key}</dt>
                      <dd className="break-all text-right text-ink">{renderValue(value)}</dd>
                    </div>
                  ))
                )}
                <div className="flex justify-between gap-3 border-t border-border-subtle pt-2">
                  <dt className="text-ink-muted">Credentials</dt>
                  <dd className="text-ink">
                    {item.has_credentials ? (
                      <Badge tone="ok">stored</Badge>
                    ) : (
                      <Badge tone="warn">none</Badge>
                    )}
                  </dd>
                </div>
              </dl>
            )}
          </AsyncBoundary>
        </Card>
      </div>

      <Card>
        <CardHeading
          title="Synchronization history"
          description="Bounded to the twenty most recent jobs."
          icon={Play}
        />
        <AsyncBoundary loading={jobs.loading} error={jobs.error} data={jobs.data} onRetry={jobs.reload}>
          {(list) =>
            list.items.length === 0 ? (
              <EmptyState
                icon={Play}
                title="No synchronization has run"
                description="Start a sync to capture records from this source."
              />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <caption className="sr-only">Synchronization history</caption>
                  <thead className="text-xs uppercase tracking-wide text-ink-faint">
                    <tr className="border-b border-border-subtle">
                      <th scope="col" className="py-2 pr-4 font-medium">Status</th>
                      <th scope="col" className="py-2 pr-4 font-medium">Attempt</th>
                      <th scope="col" className="py-2 pr-4 font-medium">Fetched</th>
                      <th scope="col" className="py-2 pr-4 font-medium">Persisted</th>
                      <th scope="col" className="py-2 pr-4 font-medium">Failed</th>
                      <th scope="col" className="py-2 pr-4 font-medium">Duration</th>
                      <th scope="col" className="py-2 pr-4 font-medium">Queued</th>
                      <th scope="col" className="py-2 font-medium">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {list.items.map((job) => (
                      <tr key={job.id} className="border-b border-border-subtle/60 last:border-0">
                        <td className="py-2.5 pr-4">
                          <Badge tone={statusTone(job.status)}>{job.status}</Badge>
                          {job.error_code ? (
                            <p className="mt-1 font-mono text-xs text-danger">{job.error_code}</p>
                          ) : null}
                        </td>
                        <td className="py-2.5 pr-4 tabular-nums text-ink-muted">{job.attempt}</td>
                        <td className="py-2.5 pr-4 tabular-nums text-ink-muted">
                          {formatNumber(job.records_fetched)}
                        </td>
                        <td className="py-2.5 pr-4 tabular-nums text-ink-muted">
                          {formatNumber(job.records_persisted)}
                        </td>
                        <td className="py-2.5 pr-4 tabular-nums text-ink-muted">
                          {formatNumber(job.records_failed)}
                        </td>
                        <td className="py-2.5 pr-4 tabular-nums text-ink-muted">
                          {formatDuration(job.duration_ms)}
                        </td>
                        <td
                          className="py-2.5 pr-4 text-ink-muted"
                          title={formatDateTime(job.queued_at)}
                        >
                          {formatRelative(job.queued_at)}
                        </td>
                        <td className="py-2.5">
                          {can("sync:cancel") && (job.status === "running" || job.status === "queued") ? (
                            <Button
                              variant="danger"
                              icon={Ban}
                              className="px-2 py-1 text-xs"
                              loading={busyAction === `cancel-${job.id}`}
                              onClick={() => {
                                void runAction(`cancel-${job.id}`, () =>
                                  api.cancelSync(connectionId, job.id),
                                );
                              }}
                              >
                              Cancel
                            </Button>
                          ) : (
                            <span className="text-xs text-ink-faint">—</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          }
        </AsyncBoundary>
      </Card>

      <Card>
        <CardHeading
          title="Captured records"
          description="A bounded preview. Payloads are untrusted source data, shown as text."
          icon={ShieldAlert}
        />
        <AsyncBoundary
          loading={records.loading}
          error={records.error}
          data={records.data}
          onRetry={records.reload}
        >
          {(list) => (
            <>
              <div className="mb-3">
                <Alert tone="info">{list.notice}</Alert>
              </div>
              {list.items.length === 0 ? (
                <EmptyState
                  icon={Database}
                  title="No records captured yet"
                  description="Records appear here after a successful synchronization."
                />
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-sm">
                    <caption className="sr-only">Captured source records</caption>
                    <thead className="text-xs uppercase tracking-wide text-ink-faint">
                      <tr className="border-b border-border-subtle">
                        <th scope="col" className="py-2 pr-4 font-medium">Source id</th>
                        <th scope="col" className="py-2 pr-4 font-medium">Type</th>
                        <th scope="col" className="py-2 pr-4 font-medium">Source timestamp</th>
                        <th scope="col" className="py-2 pr-4 font-medium">Captured</th>
                        <th scope="col" className="py-2 font-medium">Payload preview</th>
                      </tr>
                    </thead>
                    <tbody>
                      {list.items.map((record) => (
                        <tr key={record.id} className="border-b border-border-subtle/60 last:border-0">
                          <td className="py-2.5 pr-4 font-mono text-xs text-ink-muted">
                            {record.source_record_id}
                          </td>
                          <td className="py-2.5 pr-4 text-ink-muted">{record.record_type}</td>
                          <td className="py-2.5 pr-4 text-ink-muted">
                            {formatDateTime(record.source_timestamp)}
                          </td>
                          <td className="py-2.5 pr-4 text-ink-muted">
                            {formatDateTime(record.captured_at)}
                          </td>
                          <td className="py-2.5">
                            <code className="font-mono text-xs text-ink">
                              {truncate(renderValue(record.payload), 160)}
                            </code>
                            {record.payload_truncated ? (
                              <span className="ml-2 text-xs text-warn">truncated</span>
                            ) : null}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </AsyncBoundary>
      </Card>

      {can("connections:delete") ? (
        <DangerZone connectionId={connectionId} name={connection.data?.name ?? "this connection"} />
      ) : null}

      {connection.loading && connection.data ? <Spinner label="Refreshing…" /> : null}
    </div>
  );
}

function DangerZone({ connectionId, name }: { connectionId: string; name: string }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const navigate = (to: string) => {
    window.location.assign(to);
  };

  return (
    <Card className="border-danger/40">
      <CardHeading
        title="Delete this connection"
        description="Removes the connection, its captured records, and its stored credentials."
        icon={Trash2}
      />
      <div className="flex items-start gap-3">
        <Info size={16} className="mt-0.5 shrink-0 text-ink-faint" aria-hidden="true" />
        <p className="text-sm text-ink-muted">
          This is destructive and is recorded in the audit trail. Records already delivered
          to Layer 2 are governed by Layer 2's retention policy.
        </p>
      </div>
      {error ? (
        <div className="mt-3">
          <Alert tone="error">{error}</Alert>
        </div>
      ) : null}
      <div className="mt-4 flex gap-3">
        {confirming ? (
          <>
            <Button
              variant="danger"
              icon={Trash2}
              loading={busy}
              onClick={() => {
                void (async () => {
                setBusy(true);
                setError(null);
                try {
                  await api.deleteConnection(connectionId);
                  navigate("/connections");
                } catch (cause) {
                  setError(
                    cause instanceof ApiError ? cause.message : "Deletion failed.",
                  );
                  setBusy(false);
                }
                })();
              }}
            >
              Delete “{name}” permanently
            </Button>
            <Button onClick={() => setConfirming(false)}>Cancel</Button>
          </>
        ) : (
          <Button variant="danger" icon={Trash2} onClick={() => setConfirming(true)}>
            Delete connection
          </Button>
        )}
      </div>
    </Card>
  );
}
