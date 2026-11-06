/**
 * Work detail: one work entity with its provenance, relationships, and timeline.
 *
 * The page shows derived values next to where they came from. Confidence is displayed,
 * an unresolved field reads "—", and inferred relationships are labelled inferred. The
 * page never resolves a conflict for the user; that is an explicit authorized action.
 */

import { Link, useParams } from "react-router-dom";
import {
  AlertTriangle,
  ArrowLeft,
  Ban,
  Clock,
  GitBranch,
  Handshake,
  History,
  Link2,
  ListChecks,
} from "lucide-react";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { AsyncBoundary } from "../components/AsyncBoundary";
import { Badge, Card, CardHeading, DefinitionList } from "../components/ui";
import { formatDateTime, renderValue, truncate } from "../lib/format";
import {
  formatConfidence,
  label,
  priorityTone,
  verificationTone,
  workStatusTone,
} from "../lib/work";

export function WorkDetailPage() {
  const { workId = "" } = useParams();
  const state = useAsync((signal) => api.workItem(workId, signal), [workId]);

  return (
    <div className="space-y-6 p-6 lg:p-8">
      <Link
        to="/work"
        className="inline-flex items-center gap-1.5 text-sm text-ink-muted hover:text-ink"
      >
        <ArrowLeft size={15} aria-hidden="true" />
        Back to work
      </Link>

      <AsyncBoundary
        loading={state.loading}
        error={state.error}
        data={state.data}
        onRetry={state.reload}
      >
        {(work) => (
          <>
            <header className="flex flex-wrap items-start justify-between gap-4">
              <div className="min-w-0">
                <h1 className="text-xl font-semibold text-ink">{work.title}</h1>
                <p className="mt-1 font-mono text-xs text-ink-faint">{work.work_key}</p>
                {work.description ? (
                  <p className="mt-2 max-w-3xl text-sm text-ink-muted">{work.description}</p>
                ) : null}
                <div className="mt-3 flex flex-wrap gap-2">
                  <Badge tone={workStatusTone(work.status)}>{label(work.status)}</Badge>
                  <Badge tone={priorityTone(work.priority)}>
                    {label(work.priority)} · {label(work.priority_source)}
                  </Badge>
                  <Badge tone="neutral">{label(work.work_type)}</Badge>
                  <Badge tone="neutral">{work.domain}</Badge>
                  <Badge tone="info">origin: {label(work.origin)}</Badge>
                  <Badge tone={verificationTone(work.origin)}>
                    confidence: {formatConfidence(work.confidence)}
                  </Badge>
                </div>
              </div>
            </header>

            <Card>
              <CardHeading title="Facts" description="Canonical fields and source references." />
              <DefinitionList
                items={[
                  { term: "Status", detail: label(work.status) },
                  {
                    term: "Source status",
                    detail: work.source_status ? (
                      <span className="font-mono text-xs">{work.source_status}</span>
                    ) : (
                      "—"
                    ),
                  },
                  { term: "Owner", detail: work.owner_id ?? "unresolved" },
                  { term: "Assigned", detail: work.assigned_user_id ?? "unassigned" },
                  { term: "Department", detail: work.department_id ?? "unresolved" },
                  { term: "Start", detail: formatDateTime(work.start_at) },
                  {
                    term: "Due",
                    detail: work.due_at ? (
                      <>
                        {formatDateTime(work.due_at)}
                        {work.due_source ? (
                          <span className="ml-1 text-xs text-ink-faint">
                            ({label(work.due_source)})
                          </span>
                        ) : null}
                      </>
                    ) : (
                      "—"
                    ),
                  },
                  { term: "Completed", detail: formatDateTime(work.completed_at) },
                  { term: "Customer", detail: work.customer_ref ?? "—" },
                  { term: "Supplier", detail: work.supplier_ref ?? "—" },
                  { term: "Order", detail: work.order_ref ?? "—" },
                  { term: "Project", detail: work.project_ref ?? "—" },
                  { term: "Machine", detail: work.machine_ref ?? "—" },
                  { term: "Production", detail: work.production_ref ?? "—" },
                  { term: "Schema", detail: work.schema_version },
                  { term: "Updated", detail: formatDateTime(work.updated_at) },
                ]}
              />
            </Card>

            <div className="grid gap-6 lg:grid-cols-2">
              <Card>
                <CardHeading
                  title="Source references"
                  description="The records this work was derived from."
                  icon={Link2}
                />
                {work.source_references.length === 0 ? (
                  <p className="text-sm text-ink-faint">No source reference recorded.</p>
                ) : (
                  <ul className="space-y-3">
                    {work.source_references.map((reference) => (
                      <li
                        key={reference.canonical_record_id}
                        className="rounded-lg border border-border-subtle p-3 text-sm"
                      >
                        <div className="flex flex-wrap items-center gap-2">
                          <Badge tone="info">{label(reference.relationship)}</Badge>
                          <span className="font-mono text-xs text-ink">
                            {reference.source_system}
                          </span>
                          <span className="text-xs text-ink-faint">
                            confidence: {formatConfidence(reference.confidence)}
                          </span>
                        </div>
                        <p className="mt-1 text-xs text-ink-muted">
                          {reference.source_record_type} ·{" "}
                          <span className="font-mono">{reference.source_record_key}</span>
                        </p>
                        <p className="mt-1 font-mono text-xs text-ink-faint">
                          record {truncate(reference.canonical_record_id, 20)}
                        </p>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>

              <Card>
                <CardHeading
                  title="Assignments"
                  description="Participants and their roles, with verification state."
                  icon={Handshake}
                />
                {work.assignments.length === 0 ? (
                  <p className="text-sm text-ink-faint">No assignment recorded.</p>
                ) : (
                  <ul className="space-y-2">
                    {work.assignments.map((assignment, index) => (
                      <li
                        key={`${assignment.role}-${index}`}
                        className="flex flex-wrap items-center gap-2 text-sm"
                      >
                        <Badge tone={verificationTone(assignment.verification)}>
                          {label(assignment.verification)}
                        </Badge>
                        <span className="text-ink">{label(assignment.role)}</span>
                        <span className="text-ink-muted">
                          {assignment.user_id ??
                            assignment.department_id ??
                            assignment.external_name ??
                            "unresolved"}
                        </span>
                        <span className="text-xs text-ink-faint">
                          confidence: {formatConfidence(assignment.confidence)}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>
            </div>

            <Card>
              <CardHeading
                title="Commitments"
                description="Promises with a party and a due date. Verification state is explicit."
                icon={Handshake}
              />
              {work.commitments.length === 0 ? (
                <p className="text-sm text-ink-faint">No commitment recorded.</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-sm">
                    <caption className="sr-only">Commitments</caption>
                    <thead className="text-xs uppercase tracking-wide text-ink-faint">
                      <tr className="border-b border-border-subtle">
                        <th scope="col" className="py-2 pr-4 font-medium">Description</th>
                        <th scope="col" className="py-2 pr-4 font-medium">From → To</th>
                        <th scope="col" className="py-2 pr-4 font-medium">Due</th>
                        <th scope="col" className="py-2 pr-4 font-medium">Status</th>
                        <th scope="col" className="py-2 font-medium">Verified</th>
                      </tr>
                    </thead>
                    <tbody>
                      {work.commitments.map((commitment) => (
                        <tr
                          key={commitment.commitment_id}
                          className="border-b border-border-subtle/60 last:border-0"
                        >
                          <td className="py-2.5 pr-4 text-ink">{commitment.description}</td>
                          <td className="py-2.5 pr-4 text-ink-muted">
                            {commitment.committing_party} → {commitment.receiving_party}
                          </td>
                          <td className="py-2.5 pr-4 whitespace-nowrap text-ink-muted">
                            {formatDateTime(commitment.due_at)}
                          </td>
                          <td className="py-2.5 pr-4">
                            <Badge tone="neutral">{label(commitment.commitment_status)}</Badge>
                          </td>
                          <td className="py-2.5">
                            <Badge tone={verificationTone(commitment.verification_status)}>
                              {label(commitment.verification_status)}
                            </Badge>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>

            <div className="grid gap-6 lg:grid-cols-2">
              <Card>
                <CardHeading
                  title="Dependencies"
                  description="Typed edges. Inferred edges are labelled and reviewable."
                  icon={GitBranch}
                />
                {work.dependencies.length === 0 ? (
                  <p className="text-sm text-ink-faint">No dependency recorded.</p>
                ) : (
                  <ul className="space-y-2">
                    {work.dependencies.map((dependency) => (
                      <li
                        key={dependency.dependency_id}
                        className="flex flex-wrap items-center gap-2 text-sm"
                      >
                        <Badge tone="neutral">{label(dependency.relationship)}</Badge>
                        <Badge tone={verificationTone(dependency.verification_status)}>
                          {label(dependency.verification_status)}
                        </Badge>
                        <span className="text-ink-muted">
                          {dependency.target_entity_type}
                          {dependency.target_entity_ref ? (
                            <span className="ml-1 font-mono text-xs">
                              {dependency.target_entity_ref}
                            </span>
                          ) : null}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>

              <Card>
                <CardHeading
                  title="Blockers"
                  description="Why work is not progressing. Unknown reasons stay unknown."
                  icon={Ban}
                />
                {work.blockers.length === 0 ? (
                  <p className="text-sm text-ink-faint">No blocker recorded.</p>
                ) : (
                  <ul className="space-y-3">
                    {work.blockers.map((blocker) => (
                      <li
                        key={blocker.blocker_id}
                        className="rounded-lg border border-border-subtle p-3 text-sm"
                      >
                        <div className="flex flex-wrap items-center gap-2">
                          <Badge tone="warn">{label(blocker.blocker_type)}</Badge>
                          <Badge tone={verificationTone(blocker.verification_status)}>
                            {label(blocker.verification_status)}
                          </Badge>
                          <span className="text-xs text-ink-faint">
                            since {formatDateTime(blocker.detected_at)}
                          </span>
                        </div>
                        <p className="mt-1 text-ink">{blocker.description}</p>
                        {blocker.blocking_entity ? (
                          <p className="mt-1 text-xs text-ink-muted">
                            Blocking: {blocker.blocking_entity}
                          </p>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                )}
              </Card>
            </div>

            <Card>
              <CardHeading
                title="Prioritization metadata"
                description="Deterministic indicators, never a human-assigned priority."
                icon={AlertTriangle}
              />
              {work.priorities.length === 0 ? (
                <p className="text-sm text-ink-faint">No priority metadata computed.</p>
              ) : (
                <ul className="space-y-3">
                  {work.priorities.map((priority, index) => (
                    <li key={`${priority.source}-${index}`} className="text-sm">
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge tone={priorityTone(priority.level)}>{label(priority.level)}</Badge>
                        <span className="text-xs text-ink-faint">source: {priority.source}</span>
                        {priority.overdue ? <Badge tone="danger">overdue</Badge> : null}
                        {priority.due_soon ? <Badge tone="warn">due soon</Badge> : null}
                        {priority.blocked ? <Badge tone="danger">blocked</Badge> : null}
                      </div>
                      <p className="mt-1 text-xs text-ink-muted">
                        dependencies: {priority.dependency_count} · source completeness:{" "}
                        {formatConfidence(priority.source_completeness)}
                        {priority.commitment_status
                          ? ` · commitment: ${label(priority.commitment_status)}`
                          : ""}
                      </p>
                    </li>
                  ))}
                </ul>
              )}
            </Card>

            <Card>
              <CardHeading
                title="Timeline"
                description="Append-only activity history. Source, ingest, and processing times are distinct."
                icon={History}
              />
              {work.timeline.length === 0 ? (
                <p className="text-sm text-ink-faint">No timeline event recorded.</p>
              ) : (
                <ol className="space-y-3">
                  {work.timeline.map((event) => (
                    <li
                      key={event.event_id}
                      className="flex gap-3 border-b border-border-subtle/60 pb-3 last:border-0 last:pb-0"
                    >
                      <span className="mt-0.5 text-ink-faint" aria-hidden="true">
                        <Clock size={15} />
                      </span>
                      <div className="min-w-0">
                        <p className="text-sm font-medium text-ink">{label(event.event_type)}</p>
                        <p className="text-xs text-ink-muted">
                          {formatDateTime(event.event_at)} · actor {label(event.actor_type)} ·
                          provenance {label(event.provenance)}
                        </p>
                        {event.new_state ? (
                          <p className="mt-1 font-mono text-xs text-ink-faint">
                            {truncate(renderValue(event.new_state), 140)}
                          </p>
                        ) : null}
                      </div>
                    </li>
                  ))}
                </ol>
              )}
            </Card>

            {Object.keys(work.attributes).length > 0 ? (
              <Card>
                <CardHeading
                  title="Domain attributes"
                  description="Flexible attributes preserved from the source, shown verbatim."
                  icon={ListChecks}
                />
                <DefinitionList
                  items={Object.entries(work.attributes).map(([key, value]) => ({
                    term: key,
                    detail: <span className="font-mono text-xs">{renderValue(value)}</span>,
                  }))}
                />
              </Card>
            ) : null}
          </>
        )}
      </AsyncBoundary>
    </div>
  );
}
