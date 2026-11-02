/** Presentation helpers. These never interpret business meaning, only format it. */

const dateTimeFormatter = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

export function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return "—";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return "—";
  }
  return dateTimeFormatter.format(parsed);
}

export function formatRelative(value: string | null | undefined): string {
  if (!value) {
    return "never";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return "never";
  }
  const deltaSeconds = Math.round((Date.now() - parsed.getTime()) / 1000);
  if (deltaSeconds < 45) {
    return "just now";
  }
  const units: [Intl.RelativeTimeFormatUnit, number][] = [
    ["minute", 60],
    ["hour", 3600],
    ["day", 86400],
    ["month", 2592000],
    ["year", 31536000],
  ];
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  let chosen: [Intl.RelativeTimeFormatUnit, number] = units[0]!;
  for (const unit of units) {
    if (deltaSeconds >= unit[1]) {
      chosen = unit;
    }
  }
  return formatter.format(-Math.round(deltaSeconds / chosen[1]), chosen[0]);
}

export function formatNumber(value: number): string {
  return new Intl.NumberFormat().format(value);
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) {
    return "—";
  }
  if (ms < 1000) {
    return `${Math.round(ms)} ms`;
  }
  return `${(ms / 1000).toFixed(1)} s`;
}

/**
 * Present an unknown JSON value as text without ever producing markup.
 *
 * React escapes text children, so returning a string here is the safe path: a payload
 * containing `<script>` is displayed literally rather than interpreted.
 */
export function renderValue(value: unknown): string {
  if (value === null || value === undefined) {
    return "—";
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  try {
    return JSON.stringify(value);
  } catch {
    return "[unrenderable value]";
  }
}

/** Truncate long single-line values for table cells. */
export function truncate(value: string, max = 120): string {
  return value.length <= max ? value : `${value.slice(0, max - 1)}…`;
}
