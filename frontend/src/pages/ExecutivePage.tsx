/**
 * Executive work overview.
 *
 * Cross-department critical work, overdue high-priority work, and blockers. The counts
 * are counts of work in a state, never a performance score. A busy department is not
 * described as productive or unproductive here.
 */

import { RefreshCw, ShieldAlert } from "lucide-react";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { AsyncBoundary } from "../components/AsyncBoundary";
import { WorkItemTable } from "../components/WorkItemTable";
import { Badge, Button, Card, CardHeading, EmptyState, StatTile } from "../components/ui";
import { formatDateTime, formatNumber } from "../lib/format";
import { label } from "../lib/work";

export function ExecutivePage() {
  const state = useAsync((signal) => api.executiveOverview(undefined, signal), []);

  return (
    <div className="space-y-6 p-6 lg:p-8">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-ink">Executive overview</h1>
          <p className="mt-1 text-sm text-ink-muted">
            Critical work, overdue high-priority items, and persistent blockers across the
            organization.
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
        {(view) => (
          <>
            <section aria-label="Executive summary">
              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                <StatTile
                  label="Critical work"
                  value={formatNumber(view.critical_work.length)}
                  tone="danger"
                />
                <StatTile
                  label="Overdue high priority"
                  value={formatNumber(view.overdue_high_priority.length)}
                  tone={view.overdue_high_priority.length > 0 ? "danger" : "neutral"}
                />
                <StatTile
                  label="Blocked"
                  value={formatNumber(view.blocked_items.length)}
                  tone={view.blocked_items.length > 0 ? "warn" : "neutral"}
                />
              </div>
              <p className="mt-2 text-xs text-ink-faint">As of {formatDateTime(view.as_of)}</p>
            </section>

            {Object.keys(view.counts).length > 0 ? (
              <Card>
                <CardHeading
                  title="Organization counts"
                  description="Work in each state, across the organization."
                />
                <ul className="flex flex-wrap gap-2">
                  {Object.entries(view.counts).map(([key, value]) => (
                    <li key={key}>
                      <Badge tone="neutral">
                        {label(key)}: {formatNumber(value)}
                      </Badge>
                    </li>
                  ))}
                </ul>
              </Card>
            ) : null}

            <Card>
              <CardHeading
                title="Critical work"
                description="Work marked critical or urgent, with its due date."
                icon={ShieldAlert}
              />
              {view.critical_work.length === 0 ? (
                <EmptyState
                  icon={ShieldAlert}
                  title="No critical work"
                  description="Nothing is currently marked critical for this organization."
                />
              ) : (
                <WorkItemTable items={view.critical_work} caption="Critical work" />
              )}
            </Card>

            <div className="grid gap-6 lg:grid-cols-2">
              <Card>
                <CardHeading
                  title="Overdue high priority"
                  description="High-priority work past its due date."
                />
                <WorkItemTable
                  items={view.overdue_high_priority}
                  caption="Overdue high-priority work"
                  emptyLabel="No overdue high-priority work."
                />
              </Card>
              <Card>
                <CardHeading
                  title="Blocked work"
                  description="Work that cannot progress until something else is resolved."
                />
                <WorkItemTable
                  items={view.blocked_items}
                  caption="Blocked work"
                  emptyLabel="No blocked work."
                />
              </Card>
            </div>
          </>
        )}
      </AsyncBoundary>
    </div>
  );
}
