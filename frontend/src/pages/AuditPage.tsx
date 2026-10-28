/** Audit trail. Read-only, and only reachable with the `audit:read` permission. */

import { ScrollText } from "lucide-react";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { AsyncBoundary } from "../components/AsyncBoundary";
import { Badge, Button, Card, EmptyState, statusTone } from "../components/ui";
import { formatDateTime, renderValue, truncate } from "../lib/format";
import { RefreshCw } from "lucide-react";

export function AuditPage() {
  const state = useAsync((signal) => api.auditEvents({ limit: 100 }, signal), []);

  return (
    <div className="space-y-6 p-6 lg:p-8">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-ink">Audit trail</h1>
          <p className="mt-1 text-sm text-ink-muted">
            Security-sensitive actions in your organization. Records are append-only and
            cannot be edited or deleted through the application.
          </p>
        </div>
        <Button icon={RefreshCw} onClick={state.reload} loading={state.loading}>
          Refresh
        </Button>
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
              icon={ScrollText}
              title="No audit events yet"
              description="Creating a connection, running a sync, or changing a credential will appear here."
            />
          ) : (
            <Card className="p-0">
              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <caption className="sr-only">Audit events</caption>
                  <thead className="text-xs uppercase tracking-wide text-ink-faint">
                    <tr className="border-b border-border-subtle">
                      <th scope="col" className="px-5 py-3 font-medium">When</th>
                      <th scope="col" className="px-5 py-3 font-medium">Action</th>
                      <th scope="col" className="px-5 py-3 font-medium">Outcome</th>
                      <th scope="col" className="px-5 py-3 font-medium">Actor</th>
                      <th scope="col" className="px-5 py-3 font-medium">Target</th>
                      <th scope="col" className="px-5 py-3 font-medium">Metadata</th>
                    </tr>
                  </thead>
                  <tbody>
                    {list.items.map((event) => (
                      <tr key={event.id} className="border-b border-border-subtle/60 last:border-0">
                        <td className="whitespace-nowrap px-5 py-3 text-ink-muted">
                          {formatDateTime(event.created_at)}
                        </td>
                        <td className="px-5 py-3 font-mono text-xs text-ink">{event.action}</td>
                        <td className="px-5 py-3">
                          <Badge tone={statusTone(event.outcome)}>{event.outcome}</Badge>
                        </td>
                        <td className="px-5 py-3 text-ink-muted">
                          {event.actor_type}
                          {event.actor_id ? (
                            <span className="ml-1 font-mono text-xs text-ink-faint">
                              {truncate(event.actor_id, 12)}
                            </span>
                          ) : null}
                        </td>
                        <td className="px-5 py-3 text-ink-muted">
                          {event.target_type ? (
                            <>
                              {event.target_type}
                              {event.target_id ? (
                                <span className="ml-1 font-mono text-xs text-ink-faint">
                                  {truncate(event.target_id, 12)}
                                </span>
                              ) : null}
                            </>
                          ) : (
                            "—"
                          )}
                        </td>
                        <td className="px-5 py-3">
                          <code className="font-mono text-xs text-ink-faint">
                            {truncate(renderValue(event.metadata), 80)}
                          </code>
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
