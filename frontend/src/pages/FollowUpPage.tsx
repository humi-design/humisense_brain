/**
 * Follow-up center.
 *
 * Follow-ups are identified by documented, deterministic rules. A follow-up here is a
 * derived suggestion with a reason and provenance; the page never sends anything to a
 * supplier, customer, or colleague, because that action belongs to a later layer.
 */

import { Link } from "react-router-dom";
import { BellRing, RefreshCw } from "lucide-react";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { AsyncBoundary } from "../components/AsyncBoundary";
import { Badge, Button, Card, EmptyState } from "../components/ui";
import { formatDateTime, formatNumber, truncate } from "../lib/format";
import { label, urgencyTone } from "../lib/work";

export function FollowUpPage() {
  const state = useAsync((signal) => api.followUps({ limit: 100 }, signal), []);

  return (
    <div className="space-y-6 p-6 lg:p-8">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-ink">Follow-up center</h1>
          <p className="mt-1 text-sm text-ink-muted">
            Activities that need a follow-up, identified by rule. Sending the follow-up is
            not a Layer 4 action.
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
              icon={BellRing}
              title="No follow-ups required"
              description="When a supplier, customer, or department has gone quiet past a documented rule, the follow-up appears here."
            />
          ) : (
            <Card className="p-0">
              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <caption className="sr-only">Work follow-ups</caption>
                  <thead className="text-xs uppercase tracking-wide text-ink-faint">
                    <tr className="border-b border-border-subtle">
                      <th scope="col" className="px-5 py-3 font-medium">Target</th>
                      <th scope="col" className="px-5 py-3 font-medium">Reason</th>
                      <th scope="col" className="px-5 py-3 font-medium">Urgency</th>
                      <th scope="col" className="px-5 py-3 font-medium">Last activity</th>
                      <th scope="col" className="px-5 py-3 font-medium">Next due</th>
                      <th scope="col" className="px-5 py-3 font-medium">Rule</th>
                    </tr>
                  </thead>
                  <tbody>
                    {list.items.map((followUp) => (
                      <tr
                        key={followUp.follow_up_id}
                        className="border-b border-border-subtle/60 last:border-0"
                      >
                        <td className="px-5 py-3">
                          <Badge tone="info">{label(followUp.target_type)}</Badge>
                          <Link
                            to={`/work/${followUp.related_work_id}`}
                            className="ml-2 font-mono text-xs text-accent hover:underline"
                          >
                            {truncate(followUp.target_ref, 24)}
                          </Link>
                        </td>
                        <td className="px-5 py-3 text-ink-muted">{followUp.reason}</td>
                        <td className="px-5 py-3">
                          <Badge tone={urgencyTone(followUp.urgency)}>
                            {label(followUp.urgency)}
                          </Badge>
                        </td>
                        <td className="px-5 py-3 whitespace-nowrap text-ink-muted">
                          {formatDateTime(followUp.last_activity_at)}
                        </td>
                        <td className="px-5 py-3 whitespace-nowrap text-ink-muted">
                          {formatDateTime(followUp.next_due_at)}
                        </td>
                        <td className="px-5 py-3 font-mono text-xs text-ink-faint">
                          {followUp.rule_id}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="border-t border-border-subtle px-5 py-3 text-xs text-ink-faint">
                {formatNumber(list.total)} follow-up(s) total.
              </p>
            </Card>
          )
        }
      </AsyncBoundary>
    </div>
  );
}
