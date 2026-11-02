/**
 * Design-system primitives.
 *
 * Every component is keyboard reachable and carries the ARIA semantics a screen reader
 * needs. None of them is a security control: the backend authorizes every request, and
 * hiding a button here is only a courtesy.
 */

import type { ButtonHTMLAttributes, ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { AlertTriangle, CheckCircle2, Info, Loader2, ShieldAlert } from "lucide-react";

type Tone = "neutral" | "ok" | "warn" | "danger" | "info" | "accent";

const toneClasses: Record<Tone, string> = {
  neutral: "border-border-subtle bg-surface-raised text-ink-muted",
  ok: "border-ok/40 bg-ok/10 text-ok",
  warn: "border-warn/40 bg-warn/10 text-warn",
  danger: "border-danger/40 bg-danger/10 text-danger",
  info: "border-accent/40 bg-accent/10 text-accent",
  accent: "border-accent/50 bg-accent/15 text-accent",
};

export function Badge({
  children,
  tone = "neutral",
  title,
}: {
  children: ReactNode;
  tone?: Tone;
  title?: string;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-medium ${toneClasses[tone]}`}
      {...(title ? { title } : {})}
    >
      {children}
    </span>
  );
}

export function statusTone(status: string): Tone {
  switch (status) {
    case "ready":
    case "ok":
    case "succeeded":
    case "active":
    case "healthy":
      return "ok";
    case "running":
    case "queued":
    case "pending":
    case "testing":
      return "info";
    case "degraded":
    case "partial":
    case "disabled":
      return "warn";
    case "failed":
    case "error":
    case "cancelled":
      return "danger";
    default:
      return "neutral";
  }
}

export function Card({
  children,
  className = "",
  as: Tag = "section",
}: {
  children: ReactNode;
  className?: string;
  as?: "section" | "div" | "article";
}) {
  return (
    <Tag
      className={`rounded-xl border border-border-subtle bg-surface p-5 shadow-sm ${className}`}
    >
      {children}
    </Tag>
  );
}

export function CardHeading({
  title,
  description,
  action,
  icon: Icon,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
  icon?: LucideIcon;
}) {
  return (
    <div className="mb-4 flex items-start justify-between gap-4">
      <div className="flex items-start gap-3">
        {Icon ? (
          <span className="mt-0.5 rounded-lg bg-accent/10 p-2 text-accent" aria-hidden="true">
            <Icon size={18} />
          </span>
        ) : null}
        <div>
          <h2 className="text-base font-semibold text-ink">{title}</h2>
          {description ? (
            <p className="mt-1 text-sm text-ink-muted">{description}</p>
          ) : null}
        </div>
      </div>
      {action}
    </div>
  );
}

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  loading?: boolean;
  icon?: LucideIcon;
};

export function Button({
  variant = "secondary",
  loading = false,
  icon: Icon,
  children,
  className = "",
  disabled,
  type = "button",
  ...rest
}: ButtonProps) {
  const variants: Record<string, string> = {
    primary:
      "bg-accent text-canvas hover:bg-accent-strong disabled:bg-accent/40 disabled:text-canvas/60",
    secondary:
      "border border-border-subtle bg-surface-raised text-ink hover:border-accent/60 disabled:opacity-50",
    ghost: "text-ink-muted hover:bg-surface-raised hover:text-ink disabled:opacity-50",
    danger:
      "border border-danger/40 bg-danger/10 text-danger hover:bg-danger/20 disabled:opacity-50",
  };
  return (
    <button
      type={type}
      disabled={disabled === true || loading}
      aria-busy={loading || undefined}
      className={`inline-flex items-center justify-center gap-2 rounded-lg px-3.5 py-2 text-sm font-medium transition-colors disabled:cursor-not-allowed ${variants[variant]} ${className}`}
      {...rest}
    >
      {loading ? (
        <Loader2 size={16} className="animate-spin" aria-hidden="true" />
      ) : Icon ? (
        <Icon size={16} aria-hidden="true" />
      ) : null}
      {children}
    </button>
  );
}

export function Field({
  label,
  htmlFor,
  hint,
  error,
  required,
  children,
}: {
  label: string;
  htmlFor: string;
  hint?: string;
  error?: string;
  required?: boolean;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={htmlFor} className="text-sm font-medium text-ink">
        {label}
        {required ? (
          <span className="ml-1 text-danger" aria-hidden="true">
            *
          </span>
        ) : null}
      </label>
      {children}
      {hint && !error ? (
        <p id={`${htmlFor}-hint`} className="text-xs text-ink-faint">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={`${htmlFor}-error`} role="alert" className="text-xs text-danger">
          {error}
        </p>
      ) : null}
    </div>
  );
}

export const inputClasses =
  "w-full rounded-lg border border-border-subtle bg-canvas px-3 py-2 text-sm text-ink placeholder:text-ink-faint focus:border-accent focus:outline-none disabled:opacity-60";

export function Alert({
  tone,
  title,
  children,
}: {
  tone: "error" | "success" | "info" | "warning";
  title?: string;
  children: ReactNode;
}) {
  const config = {
    error: { icon: ShieldAlert, cls: "border-danger/40 bg-danger/10 text-danger" },
    success: { icon: CheckCircle2, cls: "border-ok/40 bg-ok/10 text-ok" },
    info: { icon: Info, cls: "border-accent/40 bg-accent/10 text-accent" },
    warning: { icon: AlertTriangle, cls: "border-warn/40 bg-warn/10 text-warn" },
  }[tone];
  const Icon = config.icon;
  return (
    <div
      role={tone === "error" ? "alert" : "status"}
      className={`flex items-start gap-3 rounded-lg border p-3.5 text-sm ${config.cls}`}
    >
      <Icon size={18} className="mt-0.5 shrink-0" aria-hidden="true" />
      <div className="min-w-0">
        {title ? <p className="font-semibold">{title}</p> : null}
        <div className={title ? "mt-0.5" : ""}>{children}</div>
      </div>
    </div>
  );
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <div role="status" className="flex items-center gap-2 text-sm text-ink-muted">
      <Loader2 size={16} className="animate-spin" aria-hidden="true" />
      {label}
    </div>
  );
}

export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
}: {
  icon: LucideIcon;
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 rounded-xl border border-dashed border-border-subtle px-6 py-12 text-center">
      <span className="rounded-full bg-surface-raised p-3 text-ink-faint" aria-hidden="true">
        <Icon size={22} />
      </span>
      <div>
        <p className="font-medium text-ink">{title}</p>
        <p className="mt-1 max-w-md text-sm text-ink-muted">{description}</p>
      </div>
      {action}
    </div>
  );
}

export function StatTile({
  label,
  value,
  tone = "neutral",
  icon: Icon,
}: {
  label: string;
  value: string;
  tone?: Tone;
  icon?: LucideIcon;
}) {
  return (
    <div className="rounded-xl border border-border-subtle bg-surface p-4">
      <div className="flex items-center justify-between">
        <p className="text-xs font-medium uppercase tracking-wide text-ink-faint">{label}</p>
        {Icon ? (
          <span className={toneClasses[tone].split(" ").pop()} aria-hidden="true">
            <Icon size={16} />
          </span>
        ) : null}
      </div>
      <p className="mt-2 text-2xl font-semibold tabular-nums text-ink">{value}</p>
    </div>
  );
}

export function DefinitionList({
  items,
}: {
  items: { term: string; detail: ReactNode }[];
}) {
  return (
    <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2">
      {items.map((item) => (
        <div key={item.term} className="min-w-0">
          <dt className="text-xs font-medium uppercase tracking-wide text-ink-faint">
            {item.term}
          </dt>
          <dd className="mt-0.5 break-words text-sm text-ink">{item.detail}</dd>
        </div>
      ))}
    </dl>
  );
}
