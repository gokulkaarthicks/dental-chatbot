/**
 * Pending requests: things the assistant could not resolve.
 *
 * The UI makes the approval distinction visible rather than implicit: only
 * billing/insurance-style requests show an Approve button, because only those
 * need a decision. Routine callbacks get status moves (pending → in progress →
 * resolved) and nothing else. The server enforces the same rule.
 */

import { useMemo, useState } from "react";
import { Check, Search, X } from "lucide-react";

import { staffApi, type PendingRequest, type RequestStatus, type RequestType } from "../../../api/staff";
import { Button } from "../../../components/Button";
import {
  REQUEST_TYPE_LABELS,
  describeAction,
  fullTimestamp,
  relativeTime,
} from "../../../lib/staffFormat";
import {
  requestStatusBadge,
  EmptyState,
  ErrorState,
  SkeletonRows,
} from "../StaffUi";
import { useStaffMutation, useStaffQuery } from "../useStaffData";

type StatusFilter = "open" | RequestStatus;

const STATUS_OPTIONS: { value: StatusFilter; label: string }[] = [
  { value: "open", label: "Open (pending + in progress)" },
  { value: "pending", label: "Pending" },
  { value: "in_progress", label: "In progress" },
  { value: "resolved", label: "Resolved" },
];

const TYPE_OPTIONS: { value: "" | RequestType; label: string }[] = [
  { value: "", label: "All types" },
  { value: "callback", label: "Callback" },
  { value: "reschedule", label: "Reschedule" },
  { value: "clinical_question", label: "Clinical question" },
  { value: "billing", label: "Billing / insurance" },
];

export function RequestsSection() {
  const [status, setStatus] = useState<StatusFilter>("open");
  const [requestType, setRequestType] = useState<"" | RequestType>("");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<PendingRequest | null>(null);

  const key = useMemo(
    () => JSON.stringify({ status, requestType, search: search.trim() }),
    [status, requestType, search],
  );

  const query = useStaffQuery(key, (signal) =>
    staffApi.requests(
      {
        // "open" is the union of the two unresolved states; fetch both and merge
        // so the count matches the summary tile exactly.
        status: status === "open" ? undefined : status,
        request_type: requestType || undefined,
        search: search.trim() || undefined,
      },
      signal,
    ),
  );

  const rows = (query.data?.requests ?? []).filter((row) =>
    status === "open" ? row.status !== "resolved" : true,
  );

  const pendingCount = rows.filter((row) => row.status === "pending").length;
  const approvalCount = rows.filter(
    (row) => row.approval_required && !row.approved_at && row.status !== "resolved",
  ).length;

  return (
    <div className="sd-stack">
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
        <label className="sd-toolbar__field">
          <span className="sd-field__label">Type</span>
          <select
            className="input"
            value={requestType}
            onChange={(event) => setRequestType(event.target.value as "" | RequestType)}
          >
            {TYPE_OPTIONS.map((option) => (
              <option key={option.value || "all"} value={option.value}>
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
              placeholder="Patient or request detail"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </span>
        </label>
        <div className="sd-toolbar__actions">
          <Button onClick={query.reload}>Refresh</Button>
        </div>
      </div>

      <section className="sd-panel" aria-label="Pending requests">
        <header className="sd-panel__header">
          <div>
            <h2 className="sd-panel__title">Requests needing a person</h2>
            <p className="sd-panel__sub">
              {pendingCount} pending
              {approvalCount > 0 ? ` · ${approvalCount} need approval` : ""}
              {query.refreshing ? " · refreshing" : ""}
            </p>
          </div>
        </header>

        {query.loading ? <SkeletonRows rows={6} label="Loading requests" /> : null}
        {query.error ? <ErrorState message={query.error} onRetry={query.reload} /> : null}

        {!query.loading && !query.error && rows.length === 0 ? (
          <EmptyState
            title="No requests in this view"
            hint="Routine bookings the patient confirmed never appear here. Only genuine hand-offs do."
          />
        ) : null}

        {rows.length > 0 ? (
          <div className="sd-table-wrap">
            <table className="sd-table sd-table--clickable">
              <thead>
                <tr>
                  <th scope="col">Patient</th>
                  <th scope="col">Type</th>
                  <th scope="col">Detail</th>
                  <th scope="col">Status</th>
                  <th scope="col">Received</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr
                    key={row.id}
                    className={row.status === "resolved" ? "sd-row--muted" : undefined}
                    onClick={() => setSelected(row)}
                  >
                    <td>
                      <button
                        type="button"
                        className="sd-cell-link"
                        onClick={(event) => {
                          event.stopPropagation();
                          setSelected(row);
                        }}
                      >
                        {row.patient?.full_name ?? "Unknown patient"}
                      </button>
                      <span className="sd-cell-sub">{row.patient?.phone ?? ""}</span>
                    </td>
                    <td>
                      {REQUEST_TYPE_LABELS[row.request_type]}
                      {row.approval_required ? (
                        <span className="sd-cell-sub">
                          {row.approved_at ? `Approved by ${row.approved_by ?? "staff"}` : "Approval required"}
                        </span>
                      ) : null}
                    </td>
                    <td className="sd-cell-text">{row.details}</td>
                    <td>{requestStatusBadge(row.status)}</td>
                    <td className="sd-nowrap">
                      <span className="sd-cell-sub sd-cell-sub--top" title={fullTimestamp(row.created_at)}>
                        {relativeTime(row.created_at)}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>

      {selected ? (
        <RequestDrawer
          request={selected}
          onClose={() => {
            setSelected(null);
            query.reload();
          }}
        />
      ) : null}
    </div>
  );
}

function RequestDrawer({ request, onClose }: { request: PendingRequest; onClose: () => void }) {
  const detail = useStaffQuery(`request-${request.id}`, (signal) => staffApi.request(request.id, signal));
  const [note, setNote] = useState("");
  // Close the drawer only after a successful transition; the mutation hook also
  // broadcasts, so the queue and overview counters refresh immediately.
  const mutation = useStaffMutation(() => {
    detail.reload();
    onClose();
  });

  const current = detail.data?.request ?? request;
  const canApprove = current.approval_required && !current.approved_at;

  return (
    <div className="sd-drawer-layer" role="dialog" aria-modal="true" aria-label="Request details">
      <button type="button" className="sd-scrim sd-scrim--visible" aria-label="Close" onClick={onClose} />
      <aside className="sd-drawer">
        <header className="sd-drawer__header">
          <div>
            <p className="sd-drawer__eyebrow">
              {REQUEST_TYPE_LABELS[current.request_type]} · Request #{current.id}
            </p>
            <h2 className="sd-drawer__title">{current.patient?.full_name ?? "Unknown patient"}</h2>
          </div>
          <Button variant="ghost" iconOnly size="sm" onClick={onClose} aria-label="Close" icon={<X size={16} strokeWidth={1.75} />} />
        </header>

        <div className="sd-drawer__body">
          {mutation.error ? <ErrorState message={mutation.error} /> : null}

          <div className="sd-detail-head">
            {requestStatusBadge(current.status)}
            {current.approval_required ? (
              <span className={`sd-badge sd-badge--${current.approved_at ? "ok" : "warn"}`}>
                {current.approved_at ? "Approved" : "Needs approval"}
              </span>
            ) : (
              <span className="sd-badge sd-badge--muted">No approval needed</span>
            )}
          </div>

          <blockquote className="sd-quote">{current.details}</blockquote>

          <dl className="sd-facts sd-facts--flush">
            <div>
              <dt>Phone</dt>
              <dd>{current.patient?.phone ?? "--"}</dd>
            </div>
            <div>
              <dt>Received</dt>
              <dd>{fullTimestamp(current.created_at)}</dd>
            </div>
            {current.preferred_callback_at ? (
              <div>
                <dt>Preferred callback</dt>
                <dd>{fullTimestamp(current.preferred_callback_at)}</dd>
              </div>
            ) : null}
            <div>
              <dt>Assigned to</dt>
              <dd>{current.assigned_to ?? "Unassigned"}</dd>
            </div>
            {current.conversation_id ? (
              <div>
                <dt>Conversation</dt>
                <dd className="sd-mono">{current.conversation_id}</dd>
              </div>
            ) : null}
            {current.resolution_note ? (
              <div>
                <dt>Resolution note</dt>
                <dd>{current.resolution_note}</dd>
              </div>
            ) : null}
          </dl>

          <label className="field">
            <span className="sd-field__label">Internal note (optional)</span>
            <input
              className="input"
              type="text"
              maxLength={500}
              value={note}
              onChange={(event) => setNote(event.target.value)}
            />
          </label>

          <div className="sd-actions">
            {canApprove ? (
              <Button
                variant="primary"
                icon={<Check size={15} strokeWidth={2} />}
                disabled={mutation.pending}
                onClick={() => void mutation.run(() => staffApi.approveRequest(current.id, note || undefined))}
              >
                {mutation.pending ? "Approving…" : "Approve"}
              </Button>
            ) : null}

            {current.status === "pending" ? (
              <Button
                disabled={mutation.pending}
                onClick={() => void mutation.run(() => staffApi.setRequestStatus(current.id, "in_progress"))}
              >
                Start working
              </Button>
            ) : null}

            {current.status !== "resolved" ? (
              <Button
                variant="ghost"
                disabled={mutation.pending}
                onClick={() =>
                  void mutation.run(() => staffApi.setRequestStatus(current.id, "resolved", note || undefined))
                }
              >
                Mark resolved
              </Button>
            ) : (
              <Button
                variant="ghost"
                disabled={mutation.pending}
                onClick={() => void mutation.run(() => staffApi.setRequestStatus(current.id, "in_progress"))}
              >
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
      </aside>
    </div>
  );
}
