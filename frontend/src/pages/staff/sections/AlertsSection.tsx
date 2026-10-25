/**
 * Staff alerts board.
 *
 * Emergencies and escalations. Urgency is carried by a text badge, a header
 * count, and a tinted row (never colour alone), so the board stays readable
 * for anyone who cannot distinguish the hues.
 *
 * Every acknowledge/resolve goes through the API, which persists the transition
 * with the acting staff member and writes an audit row in the same transaction.
 */

import { useMemo, useState } from "react";
import { AlertTriangle, Check, Search, X } from "lucide-react";

import { staffApi, type StaffAlert, type StaffAlertStatus } from "../../../api/staff";
import { Button } from "../../../components/Button";
import {
  ALERT_KIND_LABELS,
  alertStatusBadge,
  describeAction,
  fullTimestamp,
  relativeTime,
  severityBadge,
  EmptyState,
  ErrorState,
  SkeletonRows,
} from "../StaffUi";
import { useStaffMutation, useStaffQuery } from "../useStaffData";

type StatusFilter = "active" | StaffAlertStatus;

const STATUS_OPTIONS: { value: StatusFilter; label: string }[] = [
  { value: "active", label: "Active (open + acknowledged)" },
  { value: "open", label: "Open" },
  { value: "acknowledged", label: "Acknowledged" },
  { value: "resolved", label: "Resolved" },
];

export function AlertsSection() {
  const [status, setStatus] = useState<StatusFilter>("active");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<number | null>(null);

  const key = useMemo(() => JSON.stringify({ status, search: search.trim() }), [status, search]);

  const query = useStaffQuery(key, (signal) =>
    staffApi.alerts(
      { status: status === "active" ? undefined : status, search: search.trim() || undefined },
      signal,
    ),
  );

  const rows = (query.data?.alerts ?? []).filter((row) =>
    status === "active" ? row.status !== "resolved" : true,
  );
  const urgent = rows.filter((row) => row.is_urgent && row.status !== "resolved").length;

  return (
    <div className="sd-stack">
      {urgent > 0 ? (
        <div className="sd-urgent-banner" role="alert">
          <span className="sd-urgent-banner__icon" aria-hidden="true">
            <AlertTriangle size={16} strokeWidth={2} />
          </span>
          <div>
            <p className="sd-urgent-banner__title">
              {urgent} urgent {urgent === 1 ? "concern needs" : "concerns need"} attention
            </p>
            <p className="sd-urgent-banner__text">
              Escalated by the assistant. Acknowledge them to show someone is handling it.
            </p>
          </div>
        </div>
      ) : null}

      <div className="sd-toolbar">
        <label className="sd-toolbar__field">
          <span className="sd-field__label">Status</span>
          <select
            className="input"
            value={status}
            onChange={(event) => setStatus(event.target.value as StatusFilter)}
          >
            {STATUS_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label className="sd-toolbar__field sd-toolbar__field--grow">
          <span className="sd-field__label">Search</span>
          <span className="sd-search sd-search--sm">
            <Search size={14} strokeWidth={1.75} aria-hidden="true" className="sd-search__icon" />
            <input
              className="sd-search__input"
              type="search"
              placeholder="Patient or concern"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </span>
        </label>
        <div className="sd-toolbar__actions">
          <Button onClick={query.reload}>Refresh</Button>
        </div>
      </div>

      <section className="sd-panel" aria-label="Staff alerts">
        <header className="sd-panel__header">
          <div>
            <h2 className="sd-panel__title">Alert board</h2>
            <p className="sd-panel__sub">
              {rows.length} {rows.length === 1 ? "alert" : "alerts"}
              {query.refreshing ? " · refreshing" : ""}
            </p>
          </div>
        </header>

        {query.loading ? <SkeletonRows rows={6} label="Loading alerts" /> : null}
        {query.error ? <ErrorState message={query.error} onRetry={query.reload} /> : null}

        {!query.loading && !query.error && rows.length === 0 ? (
          <EmptyState
            title="No alerts here"
            hint="Emergencies and escalations from the chat appear on this board."
          />
        ) : null}

        {rows.length > 0 ? (
          <ul className="sd-list">
            {rows.map((alert) => {
              const active = alert.is_urgent && alert.status !== "resolved";
              return (
                <li key={alert.id}>
                  <button
                    type="button"
                    className={`sd-list__row sd-list__row--button${active ? " sd-list__row--urgent" : ""}${
                      alert.status === "resolved" ? " sd-list__row--resolved" : ""
                    }`}
                    onClick={() => setSelected(alert.id)}
                  >
                    <div className="sd-list__who">
                      <span className="sd-list__name">
                        {alert.patient?.full_name ?? alert.caller?.name ?? "Unknown caller"}
                        <span className={`sd-badge sd-badge--muted sd-list__kind`}>
                          {ALERT_KIND_LABELS[alert.kind] ?? alert.kind}
                        </span>
                        {alert.report_count > 1 ? (
                          <span className="sd-badge sd-badge--muted">{alert.report_count} reports</span>
                        ) : null}
                      </span>
                      <span className="sd-list__meta">{alert.message}</span>
                      {alert.patient?.phone ? (
                        <span className="sd-list__meta sd-list__meta--mono">{alert.patient.phone}</span>
                      ) : alert.caller?.phone ? (
                        <span className="sd-list__meta sd-list__meta--mono">
                          {alert.caller.phone} · not yet a patient
                        </span>
                      ) : null}
                    </div>
                    <div className="sd-list__side">
                      {severityBadge(alert.severity)}
                      {alertStatusBadge(alert.status)}
                      <span className="sd-list__time" title={fullTimestamp(alert.created_at)}>
                        {relativeTime(alert.created_at)}
                      </span>
                    </div>
                  </button>
                </li>
              );
            })}
          </ul>
        ) : null}
      </section>

      {selected !== null ? (
        <AlertDrawer
          alertId={selected}
          onClose={() => {
            setSelected(null);
            // Nothing changed: tell the board nothing, just re-read once.
            query.reload();
          }}
        />
      ) : null}
    </div>
  );
}

function AlertDrawer({ alertId, onClose }: { alertId: number; onClose: () => void }) {
  const detail = useStaffQuery(`alert-${alertId}`, (signal) => staffApi.alert(alertId, signal));
  const [note, setNote] = useState("");
  const mutation = useStaffMutation(() => detail.reload());

  const alert: StaffAlert | undefined = detail.data?.alert;

  function transition(next: StaffAlertStatus) {
    // The mutation hook reloads the drawer and broadcasts to the board and the
    // overview counters, so the list behind the drawer never shows the old
    // status after a successful transition.
    void mutation.run(() => staffApi.setAlertStatus(alertId, next, note || undefined));
  }

  return (
    <div className="sd-drawer-layer" role="dialog" aria-modal="true" aria-label="Alert details">
      <button type="button" className="sd-scrim sd-scrim--visible" aria-label="Close" onClick={onClose} />
      <aside className="sd-drawer">
        <header className="sd-drawer__header">
          <div>
            <p className="sd-drawer__eyebrow">
              {alert ? ALERT_KIND_LABELS[alert.kind] ?? alert.kind : "Alert"} · #{alertId}
            </p>
            <h2 className="sd-drawer__title">
              {alert?.patient?.full_name ?? alert?.caller?.name ?? "Unknown caller"}
            </h2>
          </div>
          <Button variant="ghost" iconOnly size="sm" onClick={onClose} aria-label="Close" icon={<X size={16} strokeWidth={1.75} />} />
        </header>

        {detail.loading ? <SkeletonRows rows={6} label="Loading alert" /> : null}
        {detail.error ? <ErrorState message={detail.error} onRetry={detail.reload} /> : null}

        {alert ? (
          <div className="sd-drawer__body">
            {mutation.error ? <ErrorState message={mutation.error} /> : null}

            <div className="sd-detail-head">
              {severityBadge(alert.severity)}
              {alertStatusBadge(alert.status)}
              <span className="sd-detail-head__when">raised {relativeTime(alert.created_at)}</span>
            </div>

            <blockquote className={`sd-quote${alert.is_urgent ? " sd-quote--urgent" : ""}`}>
              {alert.message}
            </blockquote>

            <dl className="sd-facts sd-facts--flush">
              <div>
                <dt>Patient</dt>
                <dd>{alert.patient?.full_name ?? "Not identified"}</dd>
              </div>
              <div>
                <dt>Phone</dt>
                <dd>{alert.patient?.phone ?? alert.caller?.phone ?? "--"}</dd>
              </div>
              {!alert.patient && alert.caller ? (
                <div>
                  <dt>Caller (unverified)</dt>
                  <dd>
                    {alert.caller.name ?? "name not given"}
                    {alert.caller.phone ? ` · ${alert.caller.phone}` : ""}
                  </dd>
                </div>
              ) : null}
              <div>
                <dt>Raised</dt>
                <dd>{fullTimestamp(alert.created_at)}</dd>
              </div>
              <div>
                <dt>Last reported</dt>
                <dd>{fullTimestamp(alert.last_reported_at ?? alert.created_at)}</dd>
              </div>
              {alert.report_count > 1 ? (
                <div>
                  <dt>Reports</dt>
                  <dd>{alert.report_count} times</dd>
                </div>
              ) : null}
              {alert.acknowledged_at ? (
                <div>
                  <dt>Acknowledged</dt>
                  <dd>{fullTimestamp(alert.acknowledged_at)}</dd>
                </div>
              ) : null}
              {alert.resolved_at ? (
                <div>
                  <dt>Resolved</dt>
                  <dd>{fullTimestamp(alert.resolved_at)}</dd>
                </div>
              ) : null}
              <div>
                <dt>Conversation</dt>
                <dd className="sd-mono">{alert.conversation_id ?? "none"}</dd>
              </div>
            </dl>

            {alert.resolution_note ? <p className="sd-hint">Resolution: {alert.resolution_note}</p> : null}

            {alert.status !== "resolved" ? (
              <label className="field">
                <span className="sd-field__label">Note (optional)</span>
                <input
                  className="input"
                  type="text"
                  maxLength={500}
                  value={note}
                  onChange={(event) => setNote(event.target.value)}
                />
              </label>
            ) : null}

            <div className="sd-actions">
              {alert.status === "open" ? (
                <Button
                  variant="primary"
                  icon={<Check size={15} strokeWidth={2} />}
                  disabled={mutation.pending}
                  onClick={() => transition("acknowledged")}
                >
                  {mutation.pending ? "Working…" : "Acknowledge"}
                </Button>
              ) : null}
              {alert.status !== "resolved" ? (
                <Button disabled={mutation.pending} onClick={() => transition("resolved")}>
                  Mark resolved
                </Button>
              ) : (
                <Button variant="ghost" disabled={mutation.pending} onClick={() => transition("open")}>
                  Reopen
                </Button>
              )}
            </div>

            <section className="sd-subsection">
              <h3 className="sd-subhead__title">Activity</h3>
              {detail.data && detail.data.activity.length === 0 ? (
                <p className="sd-hint">No staff activity recorded yet.</p>
              ) : (
                <ol className="sd-timeline">
                  {detail.data?.activity.map((entry, index) => (
                    <li key={`${entry.action}-${index}`}>
                      <span className="sd-timeline__action">{describeAction(entry.action)}</span>
                      <span className="sd-timeline__meta">
                        {entry.actor_type} #{entry.actor_id ?? "--"} · {fullTimestamp(entry.occurred_at)}
                      </span>
                    </li>
                  ))}
                </ol>
              )}
            </section>
          </div>
        ) : null}
      </aside>
    </div>
  );
}
