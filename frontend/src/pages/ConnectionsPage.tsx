/** Connections: the organization-scoped list with health and last successful sync. */

import { Link } from "react-router-dom";
import { Plus, Plug, RefreshCw } from "lucide-react";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { AsyncBoundary } from "../components/AsyncBoundary";
import { Badge, Button, Card, EmptyState, statusTone } from "../components/ui";
import { formatRelative } from "../lib/format";
import { useSession } from "../hooks/useSession";

export function ConnectionsPage() {
  const { can } = useSession();
  const state = useAsync((signal) => api.connections({ limit: 200 }, signal), []);

  return (
    <div className="space-y-6 p-6 lg:p-8">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-ink">Connections</h1>
          <p className="mt-1 text-sm text-ink-muted">
            Authorized sources for this organization. Every row is scoped to your tenant by
            the backend.
          </p>
        </div>
        <div className="flex gap-2">
          <Button icon={RefreshCw} onClick={state.reload} loading={state.loading}>
            Refresh
          </Button>
          {can("connections:create") ? (
            <Link
              to="/connections/new"
              className="inline-flex items-center gap-2 rounded-lg bg-accent px-3.5 py-2 text-sm font-medium text-canvas hover:bg-accent-strong"
            >
              <Plus size={16} aria-hidden="true" />
              Add connection
            </Link>
          ) : null}
        </div>
      </header>

      <AsyncBoundary
        loading={state.loading}
        error={state.error}
        data={state.data}
        onRetry={state.reload}
      >
        {(list) =>
          list.items.length === 0 ? (
            <EmptyState
              icon={Plug}
              title="No connections yet"
              description="A connection is one organization's configured source. Add one to begin capturing records."
              action={
                can("connections:create") ? (
                  <Link
                    to="/connections/new"
                    className="rounded-lg bg-accent px-3.5 py-2 text-sm font-medium text-canvas hover:bg-accent-strong"
                  >
                    Add a connection
                  </Link>
                ) : undefined
              }
            />
          ) : (
            <Card className="p-0">
              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <caption className="sr-only">Connections in your organization</caption>
                  <thead className="text-xs uppercase tracking-wide text-ink-faint">
                    <tr className="border-b border-border-subtle">
                      <th scope="col" className="px-5 py-3 font-medium">Name</th>
                      <th scope="col" className="px-5 py-3 font-medium">Type</th>
                      <th scope="col" className="px-5 py-3 font-medium">Status</th>
                      <th scope="col" className="px-5 py-3 font-medium">Last success</th>
                      <th scope="col" className="px-5 py-3 font-medium">Enabled</th>
                    </tr>
                  </thead>
                  <tbody>
                    {list.items.map((connection) => (
                      <tr
                        key={connection.id}
                        className="border-b border-border-subtle/60 last:border-0 hover:bg-surface-raised/50"
                      >
                        <td className="px-5 py-3">
                          <Link
                            to={`/connections/${connection.id}`}
                            className="font-medium text-accent hover:underline"
                          >
                            {connection.name}
                          </Link>
                          {connection.description ? (
                            <p className="mt-0.5 max-w-md truncate text-xs text-ink-faint">
                              {connection.description}
                            </p>
                          ) : null}
                        </td>
                        <td className="px-5 py-3 font-mono text-xs text-ink-muted">
                          {connection.connector_type}
                        </td>
                        <td className="px-5 py-3">
                          <Badge tone={statusTone(connection.status)}>{connection.status}</Badge>
                          {connection.last_error_code ? (
                            <span className="ml-2 font-mono text-xs text-danger">
                              {connection.last_error_code}
                            </span>
                          ) : null}
                        </td>
                        <td className="px-5 py-3 text-ink-muted">
                          {formatRelative(connection.last_success_at)}
                        </td>
                        <td className="px-5 py-3">
                          <Badge tone={connection.enabled ? "ok" : "warn"}>
                            {connection.enabled ? "enabled" : "disabled"}
                          </Badge>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          )
        }
      </AsyncBoundary>
    </div>
  );
}
