/**
 * My Work: the personal work view.
 *
 * Every list is computed by the backend from verified ownership and the caller's
 * authorization. The UI never widens a result: a work item appears here because the
 * server decided the caller may see it, not because a name matched.
 */

import { RefreshCw, UserCheck } from "lucide-react";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { AsyncBoundary } from "../components/AsyncBoundary";
import { WorkItemTable } from "../components/WorkItemTable";
import { Badge, Button, Card, CardHeading, EmptyState, StatTile } from "../components/ui";
import { formatDateTime, formatNumber } from "../lib/format";
import { label } from "../lib/work";

export function MyWorkPage() {
  const state = useAsync((signal) => api.myWork(undefined, signal), []);

  return (
    <div className="space-y-6 p-6 lg:p-8">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-ink">My work</h1>
          <p className="mt-1 text-sm text-ink-muted">
            Work you own, are assigned, or are waiting on. Computed from verified ownership.
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
            <section aria-label="Personal work summary">
              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                <StatTile
                  label="Assigned to me"
                  value={formatNumber(view.my_work.length)}
                  tone="accent"
                />
                <StatTile
                  label="Overdue"
                  value={formatNumber(view.overdue.length)}
                  tone={view.overdue.length > 0 ? "danger" : "neutral"}
                />
                <StatTile
                  label="Due soon"
                  value={formatNumber(view.due_soon.length)}
                  tone={view.due_soon.length > 0 ? "warn" : "neutral"}
                />
                <StatTile
                  label="Waiting on others"
                  value={formatNumber(view.waiting_on_others.length)}
                  tone="info"
                />
              </div>
              <p className="mt-2 text-xs text-ink-faint">
                As of {formatDateTime(view.as_of)}
              </p>
            </section>

            <Card>
              <CardHeading
                title="Today and next"
                description="Work assigned to you, with overdue and due-soon flags."
                icon={UserCheck}
              />
              <WorkItemTable
                items={view.my_work}
                caption="Work assigned to you"
                emptyLabel="No work is assigned to you right now."
              />
            </Card>

            <div className="grid gap-6 lg:grid-cols-2">
              <Card>
                <CardHeading
                  title="Waiting for me"
                  description="Work other people or departments are waiting on you for."
                />
                <WorkItemTable
                  items={view.waiting_for_me}
                  caption="Work waiting for you"
                  emptyLabel="Nothing is waiting on you."
                />
              </Card>
              <Card>
                <CardHeading
                  title="I am waiting for"
                  description="Work you cannot progress until someone else acts."
                />
                <WorkItemTable
                  items={view.waiting_on_others}
                  caption="Work you are waiting on"
                  emptyLabel="You are not waiting on anything."
                />
              </Card>
            </div>

            <Card>
              <CardHeading
                title="Status breakdown"
                description="Counts of your work by canonical status."
              />
              {Object.keys(view.counts).length === 0 ? (
                <EmptyState
                  icon={UserCheck}
                  title="Nothing to summarize yet"
                  description="When work is assigned to you it will be counted here by status."
                />
              ) : (
                <ul className="flex flex-wrap gap-2">
                  {Object.entries(view.counts).map(([key, value]) => (
                    <li key={key}>
                      <Badge tone="neutral">
                        {label(key)}: {formatNumber(value)}
                      </Badge>
                    </li>
                  ))}
                </ul>
              )}
            </Card>
          </>
        )}
      </AsyncBoundary>
    </div>
  );
}
