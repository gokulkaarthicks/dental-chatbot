/**
 * Formatting for the staff dashboard: API values turned into readable text.
 *
 * Everything here is pure -- no JSX, no hooks -- so a section can import one
 * label without dragging a component in. The badges that *render* state live in
 * `pages/staff/StaffUi.tsx`, which imports these label maps back, so the wording
 * and the colour of a single state are never defined in two places.
 *
 * Timestamps arrive as ISO strings and render in the viewer's local zone: a
 * receptionist reads the clock on the wall in front of them, not the server's.
 */

import type { RequestType } from "../api/staff";

export const REQUEST_TYPE_LABELS: Record<RequestType, string> = {
  callback: "Callback",
  reschedule: "Reschedule",
  clinical_question: "Clinical question",
  billing: "Billing / insurance",
};

/**
 * Alert kinds are an open string on the API, not a closed union, so an unknown
 * kind falls through to the raw value rather than rendering blank.
 */
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
