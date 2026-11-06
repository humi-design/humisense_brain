/**
 * Work list: the tenant-scoped list of work entities with filtering.
 *
 * Filters are sent to the API, which applies them inside a tenant-scoped session. The
 * page never fetches everything and filters in the browser, so a filter can never widen
 * what the caller is authorized to see.
 */

import { useState } from "react";
import { ListChecks, RefreshCw, Search } from "lucide-react";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { AsyncBoundary } from "../components/AsyncBoundary";
import { WorkItemTable } from "../components/WorkItemTable";
import { Button, Card, CardHeading, EmptyState, inputClasses } from "../components/ui";
import { formatNumber } from "../lib/format";
import type { WorkSummaryItem } from "../api/types";

const PAGE_SIZE = 25;

const STATUSES = [
  "",
  "new",
  "open",
  "assigned",
  "in_progress",
  "waiting",
  "blocked",
  "on_hold",
  "under_review",
  "completed",
  "cancelled",
] as const;

export function WorkListPage() {
  const [status, setStatus] = useState("");
  const [domain, setDomain] = useState("");
  const [search, setSearch] = useState("");
  const [applied, setApplied] = useState({ status: "", domain: "", search: "" });
  const [offset, setOffset] = useState(0);

  const state = useAsync(
    (signal) =>
      api.work(
        {
          limit: PAGE_SIZE,
          offset,
          status: applied.status || undefined,
          domain: applied.domain || undefined,
          search: applied.search || undefined,
        },
        signal,
      ),
    [applied, offset],
  );

  const items: WorkSummaryItem[] = (state.data?.items ?? []).map((entity) => ({
    work_id: entity.work_id,
    title: entity.title,
    work_type: entity.work_type,
    domain: entity.domain,
    status: entity.status,
    priority: entity.priority,
    due_at: entity.due_at,
    overdue: false,
    due_soon: false,
    blocked: entity.status === "blocked",
    owner_id: entity.owner_id,
    department_id: entity.department_id,
    updated_at: entity.updated_at,
  }));

  return (
    <div className="space-y-6 p-6 lg:p-8">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-ink">Work</h1>
          <p className="mt-1 text-sm text-ink-muted">
            Work items derived from verified source records. Every row is scoped to your
            organization by the backend.
          </p>
        </div>
        <Button icon={RefreshCw} onClick={state.reload} loading={state.loading}>
          Refresh
        </Button>
      </header>

      <Card>
        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            setOffset(0);
            setApplied({ status, domain, search });
          }}
        >
          <div className="flex flex-col gap-1.5">
            <label htmlFor="work-search" className="text-sm font-medium text-ink">
              Search
            </label>
            <input
              id="work-search"
              type="search"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Title contains…"
              className={inputClasses}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="work-status" className="text-sm font-medium text-ink">
              Status
            </label>
            <select
              id="work-status"
              value={status}
              onChange={(event) => setStatus(event.target.value)}
              className={inputClasses}
            >
              {STATUSES.map((value) => (
                <option key={value || "any"} value={value}>
                  {value === "" ? "Any status" : value.replaceAll("_", " ")}
                </option>
              ))}
            </select>
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="work-domain" className="text-sm font-medium text-ink">
              Domain
            </label>
            <input
              id="work-domain"
              type="text"
              value={domain}
              onChange={(event) => setDomain(event.target.value)}
              placeholder="Any domain"
              className={inputClasses}
            />
          </div>
          <Button type="submit" variant="primary" icon={Search}>
            Apply
          </Button>
        </form>
      </Card>

      <AsyncBoundary
        loading={state.loading}
        error={state.error}
        data={state.data}
        onRetry={state.reload}
      >
        {(list) => (
          <Card>
            <CardHeading
              title="Work items"
              description={`${formatNumber(list.total)} matching, showing ${list.items.length}.`}
              icon={ListChecks}
            />
            {list.items.length === 0 ? (
              <EmptyState
                icon={ListChecks}
                title="No work matches these filters"
                description="Work appears here once a verified source record has been derived into a work entity. Adjust the filters or run a derivation."
              />
            ) : (
              <>
                <WorkItemTable items={items} caption="Work items" />
                <div className="mt-4 flex items-center justify-between">
                  <Button
                    onClick={() => setOffset((value) => Math.max(0, value - PAGE_SIZE))}
                    disabled={offset === 0}
                  >
                    Previous
                  </Button>
                  <span className="text-xs text-ink-faint">
                    {offset + 1}–{offset + list.items.length} of {formatNumber(list.total)}
                  </span>
                  <Button
                    onClick={() => setOffset((value) => value + PAGE_SIZE)}
                    disabled={offset + PAGE_SIZE >= list.total}
                  >
                    Next
                  </Button>
                </div>
              </>
            )}
          </Card>
        )}
      </AsyncBoundary>
    </div>
  );
}
