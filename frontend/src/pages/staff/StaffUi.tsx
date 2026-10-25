/**
 * Small presentation helpers shared by the dashboard sections.
 *
 * Status tone lives in one place so "booked" looks the same in the schedule,
 * the patient record, and the overview. A badge is always text first; colour is
 * a secondary cue and every badge keeps the same shape.
 */

import type { ReactNode } from "react";

import type {
  AppointmentStatus,
  RequestStatus,
  RequestType,
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

export const REQUEST_TYPE_LABELS: Record<RequestType, string> = {
  callback: "Callback",
  reschedule: "Reschedule",
  clinical_question: "Clinical question",
  billing: "Billing / insurance",
};

export const ALERT_KIND_LABELS: Record<string, string> = {
  emergency: "Emergency",
  urgent_callback: "Urgent callback",
  insurance_verification: "Insurance verification",
  assistance_request: "Assistance request",
};

/** Human relative time for "3 min ago" style labels. */
export function relativeTime(iso: string | null): string {
  if (!iso) return "--";
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return iso;
  const seconds = Math.round((Date.now() - then.getTime()) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} hr ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days}d ago`;
  return then.toLocaleDateString();
}

/** Absolute timestamp for tooltips and detail panes. */
export function fullTimestamp(iso: string | null): string {
  if (!iso) return "--";
  const value = new Date(iso);
  if (Number.isNaN(value.getTime())) return iso;
  return value.toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
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

export function LoadingBlock({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="sd-loading" role="status" aria-live="polite">
      <span className="sd-loading__spinner" aria-hidden="true" />
      <span>{label}</span>
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

/** Audit actions are machine names; give the drawer something readable. */
export function describeAction(action: string): string {
  const map: Record<string, string> = {
    "appointment.created.staff": "Booked by staff",
    "appointment.rescheduled.staff": "Rescheduled by staff",
    "appointment.cancelled.staff": "Cancelled by staff",
    "appointment.completed.staff": "Marked completed",
    "alert.open": "Reopened",
    "alert.acknowledged": "Acknowledged",
    "alert.resolved": "Resolved",
    "alert.severity_changed": "Severity changed",
    "request.pending": "Returned to pending",
    "request.in_progress": "Picked up",
    "request.resolved": "Resolved",
    "request.approved": "Approved",
  };
  return map[action] ?? action;
}
