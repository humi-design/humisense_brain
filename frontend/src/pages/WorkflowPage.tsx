/**
 * Workflow view.
 *
 * A workflow instance is an observed state against a configurable definition. The view
 * shows completed, current, waiting, and blocked steps. It never advances a step by
 * itself: elapsed time does not complete work.
 */

import { Link } from "react-router-dom";
import { RefreshCw, Workflow } from "lucide-react";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { AsyncBoundary } from "../components/AsyncBoundary";
import { Badge, Button, Card, CardHeading, EmptyState } from "../components/ui";
import { formatNumber } from "../lib/format";
import { label, workflowStepTone } from "../lib/work";

export function WorkflowPage() {
  const state = useAsync((signal) => api.workflows({ limit: 50 }, signal), []);

  return (
    <div className="space-y-6 p-6 lg:p-8">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-ink">Workflows</h1>
          <p className="mt-1 text-sm text-ink-muted">
            Observed workflow state against configured definitions. Steps are not advanced by
            elapsed time.
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
              icon={Workflow}
              title="No workflow instances"
              description="A workflow instance appears when a work entity matches a configured workflow definition."
            />
          ) : (
            <>
              <p className="text-xs text-ink-faint">
                {formatNumber(list.total)} instance(s).
              </p>
              <div className="space-y-6">
                {list.items.map((instance) => (
                  <Card key={instance.instance_id}>
                    <CardHeading
                      title={instance.workflow_name}
                      description={`${label(instance.workflow_type)} · version ${instance.version}`}
                      icon={Workflow}
                      action={
                        <Link
                          to={`/work/${instance.work_id}`}
                          className="text-sm font-medium text-accent hover:underline"
                        >
                          View work
                        </Link>
                      }
                    />
                    <div className="mb-3 flex flex-wrap gap-2">
                      <Badge tone={workflowStepTone(instance.status)}>
                        {label(instance.status)}
                      </Badge>
                      {instance.current_step_key ? (
                        <Badge tone="info">current: {label(instance.current_step_key)}</Badge>
                      ) : null}
                    </div>
                    <ol className="space-y-2">
                      {instance.steps.map((step) => (
                        <li
                          key={step.step_key}
                          className="flex flex-wrap items-center gap-2 border-b border-border-subtle/60 pb-2 last:border-0 last:pb-0 text-sm"
                        >
                          <span className="w-6 text-xs tabular-nums text-ink-faint">
                            {step.step_order}
                          </span>
                          <Badge tone={workflowStepTone(step.observed_state)}>
                            {label(step.observed_state)}
                          </Badge>
                          <span className="text-ink">{step.step_name}</span>
                          {step.responsible_role ? (
                            <span className="text-xs text-ink-faint">
                              role: {label(step.responsible_role)}
                            </span>
                          ) : null}
                          {step.depends_on.length > 0 ? (
                            <span className="text-xs text-ink-faint">
                              depends on: {step.depends_on.map(label).join(", ")}
                            </span>
                          ) : null}
                        </li>
                      ))}
                    </ol>
                  </Card>
                ))}
              </div>
            </>
          )
        }
      </AsyncBoundary>
    </div>
  );
}
