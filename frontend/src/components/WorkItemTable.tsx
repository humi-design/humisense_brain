/** A compact work-item row shared by the personal, department, and executive views. */

import { Link } from "react-router-dom";
import { Badge } from "../components/ui";
import { formatDateTime } from "../lib/format";
import { label, priorityTone, workStatusTone } from "../lib/work";
import type { WorkSummaryItem } from "../api/types";

export function WorkItemTable({
  items,
  caption,
  emptyLabel = "No work items",
}: {
  items: WorkSummaryItem[];
  caption: string;
  emptyLabel?: string;
}) {
  if (items.length === 0) {
    return <p className="text-sm text-ink-faint">{emptyLabel}</p>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <caption className="sr-only">{caption}</caption>
        <thead className="text-xs uppercase tracking-wide text-ink-faint">
          <tr className="border-b border-border-subtle">
            <th scope="col" className="py-2 pr-4 font-medium">Work</th>
            <th scope="col" className="py-2 pr-4 font-medium">Type</th>
            <th scope="col" className="py-2 pr-4 font-medium">Status</th>
            <th scope="col" className="py-2 pr-4 font-medium">Priority</th>
            <th scope="col" className="py-2 font-medium">Due</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.work_id} className="border-b border-border-subtle/60 last:border-0">
              <td className="py-2.5 pr-4">
                <Link
                  to={`/work/${item.work_id}`}
                  className="font-medium text-accent hover:underline"
                >
                  {item.title}
                </Link>
                <div className="mt-0.5 flex flex-wrap gap-1">
                  {item.overdue ? <Badge tone="danger">overdue</Badge> : null}
                  {item.due_soon && !item.overdue ? <Badge tone="warn">due soon</Badge> : null}
                  {item.blocked ? <Badge tone="danger">blocked</Badge> : null}
                </div>
              </td>
              <td className="py-2.5 pr-4 text-ink-muted">{label(item.work_type)}</td>
              <td className="py-2.5 pr-4">
                <Badge tone={workStatusTone(item.status)}>{label(item.status)}</Badge>
              </td>
              <td className="py-2.5 pr-4">
                <Badge tone={priorityTone(item.priority)}>{label(item.priority)}</Badge>
              </td>
              <td className="py-2.5 whitespace-nowrap text-ink-muted">
                {formatDateTime(item.due_at)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
