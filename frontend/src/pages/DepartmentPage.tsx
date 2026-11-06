/**
 * Department dashboard.
 *
 * The department is chosen from the organization's own configured departments, and the
 * overview is computed by the backend for that department. Counts are counts of work in a
 * state; they are not performance measures.
 */

import { useState } from "react";
import { Building2, RefreshCw } from "lucide-react";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { AsyncBoundary } from "../components/AsyncBoundary";
import { WorkItemTable } from "../components/WorkItemTable";
import { Badge, Button, Card, CardHeading, EmptyState, StatTile, inputClasses } from "../components/ui";
import { formatDateTime, formatNumber } from "../lib/format";
import { label } from "../lib/work";

export function DepartmentPage() {
  const departments = useAsync((signal) => api.departments(signal), []);
  const [selected, setSelected] = useState("");

  const overview = useAsync(
    (signal) => api.departmentOverview(selected, undefined, signal),
    [selected],
  );

  return (
    <div className="space-y-6 p-6 lg:p-8">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-ink">Department</h1>
          <p className="mt-1 text-sm text-ink-muted">
            Workload, blockers, and cross-department dependencies for one department.
          </p>
        </div>
        <Button
          icon={RefreshCw}
          onClick={() => {
            departments.reload();
            if (selected) {
              overview.reload();
            }
          }}
          loading={departments.loading || overview.loading}
        >
          Refresh
        </Button>
      </header>

      <AsyncBoundary
        loading={departments.loading}
        error={departments.error}
        data={departments.data}
        onRetry={departments.reload}
      >
        {(list) =>
          list.items.length === 0 ? (
            <EmptyState
              icon={Building2}
              title="No departments configured"
              description="Departments are part of the tenant configuration. Once configured, their work appears here."
            />
          ) : (
            <Card>
              <label htmlFor="department" className="text-sm font-medium text-ink">
                Department
              </label>
              <select
                id="department"
                className={`mt-1.5 ${inputClasses}`}
                value={selected || list.items[0]?.department_id || ""}
                onChange={(event) => setSelected(event.target.value)}
              >
                {list.items.map((department) => (
                  <option key={department.department_id} value={department.department_id}>
                    {department.name} ({label(department.domain)})
                  </option>
                ))}
              </select>
            </Card>
          )
        }
      </AsyncBoundary>

      {selected ? (
        <AsyncBoundary
          loading={overview.loading}
          error={overview.error}
          data={overview.data}
          onRetry={overview.reload}
        >
          {(view) => (
            <>
              <section aria-label="Department summary">
                <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                  <StatTile label="Open" value={formatNumber(view.open_work)} tone="accent" />
                  <StatTile
                    label="Overdue"
                    value={formatNumber(view.overdue)}
                    tone={view.overdue > 0 ? "danger" : "neutral"}
                  />
                  <StatTile
                    label="Blocked"
                    value={formatNumber(view.blocked)}
                    tone={view.blocked > 0 ? "warn" : "neutral"}
                  />
                  <StatTile
                    label="Unassigned"
                    value={formatNumber(view.unassigned)}
                    tone={view.unassigned > 0 ? "warn" : "neutral"}
                  />
                </div>
                <p className="mt-2 text-xs text-ink-faint">As of {formatDateTime(view.as_of)}</p>
              </section>

              <Card>
                <CardHeading
                  title="Status breakdown"
                  description="Work in this department by canonical status."
                />
                {Object.keys(view.status_counts).length === 0 ? (
                  <p className="text-sm text-ink-faint">No work in this department.</p>
                ) : (
                  <ul className="flex flex-wrap gap-2">
                    {Object.entries(view.status_counts).map(([key, value]) => (
                      <li key={key}>
                        <Badge tone="neutral">
                          {label(key)}: {formatNumber(value)}
                        </Badge>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>

              <Card>
                <CardHeading
                  title="Work items"
                  description="Open work for this department, with overdue and blocked flags."
                />
                <WorkItemTable
                  items={view.items}
                  caption="Department work items"
                  emptyLabel="No open work for this department."
                />
              </Card>
            </>
          )}
        </AsyncBoundary>
      ) : null}
    </div>
  );
}
