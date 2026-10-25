/**
 * Overview section: the numbers a front desk checks first, then the two queues
 * that need a decision today.
 *
 * Summary counts come from one server-side query, so the tiles can never
 * disagree with the tables on the other tabs.
 */

import { ArrowRight } from "lucide-react";

import { staffApi, type StaffAlert, type StaffAppointment, type SummaryCounts } from "../../../api/staff";
import {
  REQUEST_TYPE_LABELS,
  relativeTime,
  severityBadge,
  EmptyState,
  ErrorState,
  SkeletonRows,
} from "../StaffUi";
import type { StaffView } from "../StaffDashboard";
import { useStaffQuery } from "../useStaffData";

const EMPTY_COUNTS: SummaryCounts = {
  appointments_today: 0,
  appointments_upcoming: 0,
  open_alerts: 0,
  urgent_alerts: 0,
  pending_requests: 0,
  approvals_required: 0,
};

export function OverviewSection({ onNavigate }: { onNavigate: (view: StaffView) => void }) {
  const summary = useStaffQuery("summary", () => staffApi.summary());
  const today = useStaffQuery("today-appointments", () => staffApi.appointments({ upcomingOnly: true }));
  const alerts = useStaffQuery("open-alerts", () => staffApi.alerts({ status: "open" }));
  const requests = useStaffQuery("open-requests", () => staffApi.requests({ status: "pending" }));

  const counts = summary.data?.counts ?? EMPTY_COUNTS;
  const attentionBusy = alerts.loading || requests.loading;

  return (
    <div className="sd-stack">
      <section className="sd-stats" aria-label="Summary">
        <Stat
          label="Appointments today"
          value={counts.appointments_today}
          hint={`${counts.appointments_upcoming} upcoming`}
          loading={summary.loading}
          attention={false}
          onClick={() => onNavigate("appointments")}
        />
        <Stat
          label="Open alerts"
          value={counts.open_alerts}
          hint={counts.urgent_alerts > 0 ? `${counts.urgent_alerts} urgent` : "None urgent"}
          loading={summary.loading}
          attention={counts.urgent_alerts > 0}
          onClick={() => onNavigate("alerts")}
        />
        <Stat
          label="Pending requests"
          value={counts.pending_requests}
          hint="Awaiting a reply"
          loading={summary.loading}
          attention={false}
          onClick={() => onNavigate("requests")}
        />
        <Stat
          label="Approvals required"
          value={counts.approvals_required}
          hint="Insurance / billing"
          loading={summary.loading}
          attention={counts.approvals_required > 0}
          onClick={() => onNavigate("requests")}
        />
      </section>

      {summary.error ? <ErrorState message={summary.error} onRetry={summary.reload} /> : null}

      <div className="sd-split">
        <section className="sd-panel" aria-label="Next appointments">
          <header className="sd-panel__header">
            <div>
              <h2 className="sd-panel__title">Coming up</h2>
              <p className="sd-panel__sub">Next booked visits, soonest first</p>
            </div>
            <button type="button" className="sd-textbtn" onClick={() => onNavigate("appointments")}>
              View all
              <ArrowRight size={13} strokeWidth={1.75} aria-hidden="true" />
            </button>
          </header>

          {today.loading ? <SkeletonRows rows={5} label="Loading schedule" /> : null}
          {today.error ? <ErrorState message={today.error} onRetry={today.reload} /> : null}
          {!today.loading && !today.error && (today.data?.appointments.length ?? 0) === 0 ? (
            <EmptyState title="No upcoming appointments" hint="New bookings will appear here." />
          ) : null}
          {today.data && today.data.appointments.length > 0 ? (
            <ul className="sd-list">
              {today.data.appointments.slice(0, 5).map((row: StaffAppointment) => (
                <li key={row.id} className="sd-list__row">
                  <div className="sd-list__when">
                    <span className="sd-list__day">{row.day_label}</span>
                    <span className="sd-list__time">{row.time_label}</span>
                  </div>
                  <div className="sd-list__who">
                    <span className="sd-list__name">{row.patient?.full_name ?? "Patient"}</span>
                    <span className="sd-list__meta">
                      {row.appointment_type?.name ?? "Visit"} · {row.provider?.full_name ?? "Provider"}
                    </span>
                  </div>
                </li>
              ))}
            </ul>
          ) : null}
        </section>

        <section className="sd-panel" aria-label="Needs attention">
          <header className="sd-panel__header">
            <div>
              <h2 className="sd-panel__title">Needs attention</h2>
              <p className="sd-panel__sub">Open alerts and new requests</p>
            </div>
            <button type="button" className="sd-textbtn" onClick={() => onNavigate("alerts")}>
              Alert board
              <ArrowRight size={13} strokeWidth={1.75} aria-hidden="true" />
            </button>
          </header>

          {attentionBusy ? <SkeletonRows rows={4} label="Loading queue" /> : null}
          {alerts.error ? <ErrorState message={alerts.error} onRetry={alerts.reload} /> : null}
          {requests.error ? <ErrorState message={requests.error} onRetry={requests.reload} /> : null}

          {!attentionBusy &&
          !alerts.error &&
          (alerts.data?.alerts.length ?? 0) === 0 &&
          (requests.data?.requests.length ?? 0) === 0 ? (
            <EmptyState title="Nothing needs attention" hint="Open alerts and new requests appear here." />
          ) : null}

          <ul className="sd-list">
            {alerts.data?.alerts.slice(0, 3).map((alert: StaffAlert) => (
              <li
                key={`alert-${alert.id}`}
                className={`sd-list__row${alert.is_urgent ? " sd-list__row--urgent" : ""}`}
              >
                <div className="sd-list__who">
                  <span className="sd-list__name">
                    {alert.patient?.full_name ?? alert.caller?.name ?? "Unknown caller"}
                  </span>
                  <span className="sd-list__meta">{alert.message}</span>
                </div>
                <div className="sd-list__side">
                  {severityBadge(alert.severity)}
                  <span className="sd-list__time">{relativeTime(alert.created_at)}</span>
                </div>
              </li>
            ))}
            {requests.data?.requests.slice(0, 3).map((request) => (
              <li key={`req-${request.id}`} className="sd-list__row">
                <div className="sd-list__who">
                  <span className="sd-list__name">
                    {request.patient?.full_name ?? "Patient"}
                    <span className="sd-list__sep"> · </span>
                    <span className="sd-list__meta">{REQUEST_TYPE_LABELS[request.request_type]}</span>
                  </span>
                  <span className="sd-list__meta">{request.details}</span>
                </div>
                <div className="sd-list__side">
                  {request.approval_required ? (
                    <span className="sd-badge sd-badge--warn">Approval</span>
                  ) : null}
                  <span className="sd-list__time">{relativeTime(request.created_at)}</span>
                </div>
              </li>
            ))}
          </ul>
        </section>
      </div>
    </div>
  );
}

/**
 * A summary figure. The four tiles share one surface and are divided by
 * hairlines rather than padded individually, which is quieter and keeps the row
 * aligned to the panels below.
 */
function Stat({
  label,
  value,
  hint,
  loading,
  attention,
  onClick,
}: {
  label: string;
  value: number;
  hint: string;
  loading: boolean;
  attention: boolean;
  onClick: () => void;
}) {
  return (
    <button type="button" className="sd-stat" onClick={onClick}>
      <span className="sd-stat__label">{label}</span>
      {loading ? (
        <span className="skel" style={{ width: 40, height: 24, margin: "2px 0 4px" }} />
      ) : (
        <span className="sd-stat__value">{value}</span>
      )}
      <span className={`sd-stat__hint${attention ? " sd-stat__hint--attention" : ""}`}>
        {attention ? <span className="sd-stat__dot" aria-hidden="true" /> : null}
        {hint}
      </span>
    </button>
  );
}
