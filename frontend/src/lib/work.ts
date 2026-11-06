/**
 * Presentation helpers for work intelligence.
 *
 * These map canonical values to a visual tone and a human label. They never invent a
 * meaning: an `unknown` status stays `unknown`, an `unresolved` verification stays
 * `unresolved`, and an inferred value is labelled inferred wherever it is shown.
 */

export type Tone = "neutral" | "ok" | "warn" | "danger" | "info" | "accent";

export function workStatusTone(status: string): Tone {
  switch (status) {
    case "completed":
      return "ok";
    case "cancelled":
      return "neutral";
    case "blocked":
      return "danger";
    case "waiting":
    case "on_hold":
      return "warn";
    case "in_progress":
    case "under_review":
      return "info";
    case "assigned":
    case "open":
    case "new":
      return "accent";
    default:
      return "neutral";
  }
}

export function priorityTone(level: string): Tone {
  switch (level) {
    case "critical":
    case "urgent":
      return "danger";
    case "high":
      return "warn";
    case "low":
      return "neutral";
    default:
      return "info";
  }
}

export function urgencyTone(urgency: string): Tone {
  switch (urgency) {
    case "urgent":
      return "danger";
    case "high":
      return "warn";
    case "low":
      return "neutral";
    default:
      return "info";
  }
}

export function verificationTone(verification: string): Tone {
  switch (verification) {
    case "verified":
      return "ok";
    case "source_reported":
      return "info";
    case "inferred":
      return "warn";
    default:
      return "neutral";
  }
}

export function workflowStepTone(state: string): Tone {
  switch (state) {
    case "completed":
      return "ok";
    case "active":
      return "info";
    case "blocked":
      return "danger";
    case "waiting":
      return "warn";
    case "skipped":
      return "neutral";
    default:
      return "neutral";
  }
}

/** `blocked`/`waiting` are the two statuses that read as "not progressing". */
export function isWaiting(status: string): boolean {
  return status === "waiting" || status === "blocked";
}

/**
 * Render a confidence as a percentage, or "—" when unknown.
 *
 * A missing confidence is shown as unknown rather than defaulted, because presenting an
 * inferred value as certain is the exact error this layer avoids.
 */
export function formatConfidence(value: number | null | undefined): string {
  if (value === null || value === undefined) {
    return "—";
  }
  return `${Math.round(value * 100)}%`;
}

/** Human label for a snake_case identifier. */
export function label(value: string): string {
  return value.replaceAll("_", " ");
}

/** Order used when listing status counts, so the UI is stable between renders. */
export const STATUS_ORDER = [
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
  "unknown",
] as const;
