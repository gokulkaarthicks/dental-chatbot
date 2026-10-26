/**
 * Badges and layout states shared by the dashboard sections.
 *
 * Status tone lives in one place so "booked" looks the same in the schedule,
 * the patient record, and the overview. A badge is always text first; colour is
 * a secondary cue and every badge keeps the same shape.
 *
 * Only things that render belong here. Pure text formatting -- relative times,
 * audit labels, enum wording reused by a table cell -- lives in
 * `lib/staffFormat.ts`, so a section can pick up one label without importing
 * components it does not draw.
 */

import type { ReactNode } from "react";

import type {
  AppointmentStatus,
  RequestStatus,
  StaffAlertSeverity,
  StaffAlertStatus,
} from "../../api/staff";

type Tone = "ok" | "warn" | "danger" | "muted" | "accent";

function Badge({ tone = "muted", children }: { tone?: Tone; children: ReactNode }) {
  return <span className={`sd-badge sd-badge--${tone}`}>{children}</span>;
}

const APPOINTMENT_LABELS: Record<AppointmentStatus, string> = {
  booked: "Booked",
  rescheduled: "Rescheduled",
  cancelled: "Cancelled",
  completed: "Completed",
};

export function appointmentBadge(status: AppointmentStatus) {
  const tone: Tone = status === "booked" ? "accent" : status === "cancelled" ? "danger" : "muted";
  return <Badge tone={tone}>{APPOINTMENT_LABELS[status]}</Badge>;
}

const ALERT_LABELS: Record<StaffAlertStatus, string> = {
  open: "Open",
  acknowledged: "Acknowledged",
  resolved: "Resolved",
};

export function alertStatusBadge(status: StaffAlertStatus) {
  const tone: Tone = status === "resolved" ? "ok" : status === "acknowledged" ? "muted" : "warn";
  return <Badge tone={tone}>{ALERT_LABELS[status]}</Badge>;
}

const SEVERITY_LABELS: Record<StaffAlertSeverity, string> = {
  urgent: "Urgent",
  high: "High",
  normal: "Normal",
  low: "Low",
};

export function severityBadge(severity: StaffAlertSeverity) {
  const tone: Tone =
    severity === "urgent" || severity === "high" ? "danger" : severity === "normal" ? "warn" : "muted";
  return (
    <Badge tone={tone}>
      <span className="sd-badge__dot" aria-hidden="true" />
      {SEVERITY_LABELS[severity]}
    </Badge>
  );
}

const REQUEST_LABELS: Record<RequestStatus, string> = {
  pending: "Pending",
  in_progress: "In progress",
  resolved: "Resolved",
};

export function requestStatusBadge(status: RequestStatus) {
  const tone: Tone = status === "resolved" ? "ok" : status === "in_progress" ? "accent" : "warn";
  return <Badge tone={tone}>{REQUEST_LABELS[status]}</Badge>;
}

/**
 * Skeleton placeholders. They hold the exact height of the rows they replace so
 * the layout does not jump when data lands, and stay low-opacity so they never
 * compete with real content.
 */
export function SkeletonRows({ rows = 4, label = "Loading" }: { rows?: number; label?: string }) {
  return (
    <div className="sd-skeleton" role="status" aria-live="polite">
      <span className="sd-visually-hidden">{label}</span>
      {Array.from({ length: rows }).map((_, index) => (
        <div className="sd-skeleton__row" key={index}>
          <span className="skel" style={{ width: "18%", height: 12 }} />
          <span className="skel" style={{ width: "26%", height: 12 }} />
          <span className="skel" style={{ width: "22%", height: 12 }} />
          <span className="skel" style={{ width: "12%", height: 18 }} />
        </div>
      ))}
    </div>
  );
}

export function EmptyState({
  title,
  hint,
  action,
}: {
  title: string;
  hint?: string;
  action?: ReactNode;
}) {
  return (
    <div className="sd-empty">
      <p className="sd-empty__title">{title}</p>
      {hint ? <p className="sd-empty__hint">{hint}</p> : null}
      {action}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="sd-error" role="alert">
      <p>{message}</p>
      {onRetry ? (
        <button type="button" className="btn btn--ghost btn--sm" onClick={onRetry}>
          Try again
        </button>
      ) : null}
    </div>
  );
}
