/**
 * Appointments section.
 *
 * A filter bar, a dense schedule table, and a detail drawer. Reads and writes go
 * through the staff API, which validates every change against the same
 * availability engine the patient chatbot uses. After any mutation the list and
 * the open drawer are refetched, so the row on screen is whatever the database
 * now says -- not what we hoped it would say.
 */

import { useEffect, useMemo, useState } from "react";
import { CalendarDays, Plus, Search, X } from "lucide-react";

import {
  staffApi,
  type StaffAppointment,
  type AppointmentStatus,
  type SlotOption,
} from "../../../api/staff";
import { Button } from "../../../components/Button";
import {
  appointmentBadge,
  describeAction,
  fullTimestamp,
  EmptyState,
  ErrorState,
  SkeletonRows,
} from "../StaffUi";
import { useStaffMutation, useStaffQuery } from "../useStaffData";

type StatusFilter = "all" | AppointmentStatus;

const STATUS_OPTIONS: { value: StatusFilter; label: string }[] = [
  { value: "all", label: "All statuses" },
  { value: "booked", label: "Booked" },
  { value: "completed", label: "Completed" },
  { value: "cancelled", label: "Cancelled" },
];

export function AppointmentsSection() {
  const [day, setDay] = useState("");
  const [providerId, setProviderId] = useState<number | null>(null);
  const [status, setStatus] = useState<StatusFilter>("all");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<number | null>(null);
  const [booking, setBooking] = useState(false);

  const key = useMemo(
    () => JSON.stringify({ day, providerId, status, search: search.trim() }),
    [day, providerId, status, search],
  );

  const query = useStaffQuery(key, (signal) =>
    staffApi.appointments(
      {
        day: day || undefined,
        providerId,
        status: status === "all" ? undefined : status,
        search: search.trim() || undefined,
      },
      signal,
    ),
  );

  const providers = query.data?.providers ?? [];
  const rows = query.data?.appointments ?? [];
  const hasFilters = Boolean(day || providerId || status !== "all" || search.trim());

  function resetFilters() {
    setDay("");
    setProviderId(null);
    setStatus("all");
    setSearch("");
  }

  const count = rows.length;

  return (
    <div className="sd-stack">
      <div className="sd-toolbar">
        <label className="sd-toolbar__field">
          <span className="sd-field__label">Date</span>
          <input
            className="input"
            type="date"
            value={day}
            onChange={(event) => setDay(event.target.value)}
          />
        </label>

        <label className="sd-toolbar__field">
          <span className="sd-field__label">Dentist</span>
          <select
            className="input"
            value={providerId ?? ""}
            onChange={(event) => setProviderId(event.target.value ? Number(event.target.value) : null)}
          >
            <option value="">All dentists</option>
            {providers.map((provider) => (
              <option key={provider.id} value={provider.id}>
                {provider.full_name}
              </option>
            ))}
          </select>
        </label>

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
          <span className="sd-field__label">Patient</span>
          <span className="sd-search sd-search--sm">
            <Search size={14} strokeWidth={1.75} aria-hidden="true" className="sd-search__icon" />
            <input
              className="sd-search__input"
              type="search"
              placeholder="Name or phone"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </span>
        </label>

        <div className="sd-toolbar__actions">
          <Button
            variant="primary"
            icon={<Plus size={15} strokeWidth={2} />}
            onClick={() => setBooking(true)}
          >
            New appointment
          </Button>
        </div>
      </div>

      <section className="sd-panel" aria-label="Appointments">
        <header className="sd-panel__header">
          <div>
            <h2 className="sd-panel__title">Schedule</h2>
            <p className="sd-panel__sub">
              {query.loading ? "Loading…" : `${count} ${count === 1 ? "appointment" : "appointments"}`}
              {query.refreshing ? " · refreshing" : ""}
            </p>
          </div>
          <div className="sd-panel__actions">
            {hasFilters ? (
              <Button size="sm" variant="ghost" onClick={resetFilters} icon={<X size={13} strokeWidth={2} />}>
                Clear filters
              </Button>
            ) : null}
            <Button size="sm" onClick={query.reload}>
              Refresh
            </Button>
          </div>
        </header>

        {query.loading ? <SkeletonRows rows={6} label="Loading appointments" /> : null}
        {query.error ? <ErrorState message={query.error} onRetry={query.reload} /> : null}

        {!query.loading && !query.error && rows.length === 0 ? (
          <EmptyState
            title={hasFilters ? "No appointments match these filters" : "No appointments yet"}
            hint={
              hasFilters
                ? "Try a different date, dentist, or status."
                : "Book the first visit with “New appointment”."
            }
          />
        ) : null}

        {rows.length > 0 ? (
          <div className="sd-table-wrap">
            <table className="sd-table sd-table--clickable">
              <thead>
                <tr>
                  <th scope="col">Patient</th>
                  <th scope="col">When</th>
                  <th scope="col">Visit</th>
                  <th scope="col">Dentist</th>
                  <th scope="col">Status</th>
                  <th scope="col">Added</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr
                    key={row.id}
                    className={row.status === "cancelled" ? "sd-row--muted" : undefined}
                    onClick={() => setSelected(row.id)}
                  >
                    <td>
                      <button
                        type="button"
                        className="sd-cell-link"
                        onClick={(event) => {
                          event.stopPropagation();
                          setSelected(row.id);
                        }}
                      >
                        {row.patient?.full_name ?? "Unknown patient"}
                      </button>
                      <span className="sd-cell-sub">{row.patient?.phone ?? ""}</span>
                    </td>
                    <td className="sd-nowrap">
                      {row.day_label}
                      <span className="sd-cell-sub">
                        {row.time_label} – {row.end_time_label}
                      </span>
                    </td>
                    <td>
                      {row.appointment_type?.name ?? "Visit"}
                      <span className="sd-cell-sub">{row.duration_minutes} min</span>
                    </td>
                    <td>{row.provider?.full_name ?? "--"}</td>
                    <td>{appointmentBadge(row.status)}</td>
                    <td className="sd-nowrap">
                      <span className="sd-cell-sub sd-cell-sub--top" title={fullTimestamp(row.created_at)}>
                        {fullTimestamp(row.created_at)}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>

      {selected !== null ? (
        <AppointmentDrawer
          appointmentId={selected}
          onClose={() => {
            setSelected(null);
            query.reload();
          }}
          onChanged={query.reload}
        />
      ) : null}

      {booking ? (
        <BookingModal
          onClose={() => setBooking(false)}
          onCreated={() => {
            setBooking(false);
            query.reload();
          }}
          defaultDay={day || undefined}
        />
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Detail drawer                                                       */
/* ------------------------------------------------------------------ */

function AppointmentDrawer({
  appointmentId,
  onClose,
  onChanged,
}: {
  appointmentId: number;
  onClose: () => void;
  onChanged: () => void;
}) {
  const detail = useStaffQuery(`appointment-${appointmentId}`, (signal) =>
    staffApi.appointment(appointmentId, signal),
  );
  const [mode, setMode] = useState<"view" | "reschedule" | "cancel">("view");
  const [newTime, setNewTime] = useState("");
  const [reason, setReason] = useState("");

  const mutation = useStaffMutation(() => {
    onChanged();
    detail.reload();
    setMode("view");
    setReason("");
    // The drawer's own move/cancel changes availability for the booking modal
    // and every other list; the mutation hook already broadcast that.
  });

  const appointment: StaffAppointment | undefined = detail.data?.appointment;

  return (
    <div className="sd-drawer-layer" role="dialog" aria-modal="true" aria-label="Appointment details">
      <button type="button" className="sd-scrim sd-scrim--visible" aria-label="Close" onClick={onClose} />
      <aside className="sd-drawer">
        <header className="sd-drawer__header">
          <div>
            <p className="sd-drawer__eyebrow">Appointment #{appointmentId}</p>
            <h2 className="sd-drawer__title">{appointment?.patient?.full_name ?? "Loading…"}</h2>
          </div>
          <Button variant="ghost" iconOnly size="sm" onClick={onClose} aria-label="Close details" icon={<X size={16} strokeWidth={1.75} />} />
        </header>

        {detail.loading ? <SkeletonRows rows={6} label="Loading appointment" /> : null}
        {detail.error ? <ErrorState message={detail.error} onRetry={detail.reload} /> : null}

        {appointment ? (
          <div className="sd-drawer__body">
            {mutation.error ? <ErrorState message={mutation.error} /> : null}

            <div className="sd-detail-head">
              {appointmentBadge(appointment.status)}
              <span className="sd-detail-head__when">
                {appointment.day_label} · {appointment.time_label}–{appointment.end_time_label}
              </span>
            </div>

            <dl className="sd-facts sd-facts--flush">
              <div>
                <dt>Visit type</dt>
                <dd>
                  {appointment.appointment_type?.name ?? "--"} ({appointment.duration_minutes} min)
                </dd>
              </div>
              <div>
                <dt>Dentist</dt>
                <dd>
                  {appointment.provider
                    ? `${appointment.provider.full_name}, ${appointment.provider.credentials}`
                    : "--"}
                </dd>
              </div>
              <div>
                <dt>Created</dt>
                <dd>{fullTimestamp(appointment.created_at)}</dd>
              </div>
              <div>
                <dt>Last updated</dt>
                <dd>{fullTimestamp(appointment.updated_at)}</dd>
              </div>
              {appointment.cancelled_at ? (
                <div>
                  <dt>Cancelled</dt>
                  <dd>{fullTimestamp(appointment.cancelled_at)}</dd>
                </div>
              ) : null}
              {appointment.reason ? (
                <div>
                  <dt>Notes</dt>
                  <dd>{appointment.reason}</dd>
                </div>
              ) : null}
            </dl>

            <section className="sd-subsection">
              <h3 className="sd-subhead__title">Patient</h3>
              <dl className="sd-facts sd-facts--flush">
                <div>
                  <dt>Phone</dt>
                  <dd>{appointment.patient?.phone ?? "--"}</dd>
                </div>
                <div>
                  <dt>Date of birth</dt>
                  <dd>{appointment.patient?.date_of_birth ?? "--"}</dd>
                </div>
                <div>
                  <dt>Insurance</dt>
                  <dd>{appointment.patient?.insurance_provider ?? "Self pay"}</dd>
                </div>
                {appointment.patient?.insurance_member_id ? (
                  <div>
                    <dt>Member ID</dt>
                    <dd className="sd-mono">{appointment.patient.insurance_member_id}</dd>
                  </div>
                ) : null}
              </dl>
            </section>

            {appointment.status === "booked" ? (
              <section className="sd-subsection">
                <h3 className="sd-subhead__title">Actions</h3>
                {mode === "view" ? (
                  <div className="sd-actions">
                    <Button onClick={() => setMode("reschedule")}>Reschedule</Button>
                    <Button variant="danger" onClick={() => setMode("cancel")}>
                      Cancel appointment
                    </Button>
                    <Button
                      variant="ghost"
                      disabled={mutation.pending}
                      onClick={() => void mutation.run(() => staffApi.complete(appointment.id))}
                    >
                      Mark completed
                    </Button>
                  </div>
                ) : null}

                {mode === "reschedule" ? (
                  <div className="sd-inline-form">
                    <p className="sd-inline-form__title">Move to a new time</p>
                    <label className="field">
                      <span className="sd-field__label">New start (clinic time)</span>
                      <input
                        className="input"
                        type="datetime-local"
                        value={newTime}
                        onChange={(event) => setNewTime(event.target.value)}
                      />
                    </label>
                    <p className="sd-hint">
                      The move only succeeds if that interval is genuinely free. If it is not, the
                      original booking is unchanged and the reason is shown here.
                    </p>
                    <div className="sd-actions">
                      <Button
                        variant="primary"
                        disabled={!newTime || mutation.pending}
                        onClick={() =>
                          void mutation.run(() =>
                            staffApi.reschedule(appointment.id, {
                              starts_at: newTime,
                            }),
                          )
                        }
                      >
                        {mutation.pending ? "Moving…" : "Confirm reschedule"}
                      </Button>
                      <Button variant="ghost" onClick={() => setMode("view")}>
                        Back
                      </Button>
                    </div>
                  </div>
                ) : null}

                {mode === "cancel" ? (
                  <div className="sd-inline-form sd-inline-form--danger">
                    <p className="sd-inline-form__title">Cancel this appointment?</p>
                    <label className="field">
                      <span className="sd-field__label">Reason (optional)</span>
                      <input
                        className="input"
                        type="text"
                        value={reason}
                        maxLength={500}
                        onChange={(event) => setReason(event.target.value)}
                      />
                    </label>
                    <p className="sd-hint">
                      The slot is released immediately and becomes bookable again.
                    </p>
                    <div className="sd-actions">
                      <Button
                        variant="danger"
                        disabled={mutation.pending}
                        onClick={() =>
                          void mutation.run(() => staffApi.cancel(appointment.id, reason || undefined))
                        }
                      >
                        {mutation.pending ? "Cancelling…" : "Cancel appointment"}
                      </Button>
                      <Button variant="ghost" onClick={() => setMode("view")}>
                        Keep it
                      </Button>
                    </div>
                  </div>
                ) : null}
              </section>
            ) : null}

            <section className="sd-subsection">
              <h3 className="sd-subhead__title">Audit trail</h3>
              {detail.data && detail.data.history.length === 0 ? (
                <p className="sd-hint">No staff actions recorded for this appointment.</p>
              ) : (
                <ol className="sd-timeline">
                  {detail.data?.history.map((entry, index) => (
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

/* ------------------------------------------------------------------ */
/* New booking                                                         */
/* ------------------------------------------------------------------ */

function BookingModal({
  onClose,
  onCreated,
  defaultDay,
}: {
  onClose: () => void;
  onCreated: () => void;
  defaultDay?: string;
}) {
  const [patientQuery, setPatientQuery] = useState("");
  const [patientResults, setPatientResults] = useState<{ id: number; full_name: string; phone: string }[]>([]);
  const [patient, setPatient] = useState<{ id: number; full_name: string; phone: string } | null>(null);
  const [providerId, setProviderId] = useState<number | null>(null);
  const [appointmentType, setAppointmentType] = useState("checkup");
  const [day, setDay] = useState(defaultDay ?? new Date().toISOString().slice(0, 10));
  const [slots, setSlots] = useState<SlotOption[]>([]);
  const [slotsLoaded, setSlotsLoaded] = useState(false);
  const [slotsLoading, setSlotsLoading] = useState(false);
  const [slotsError, setSlotsError] = useState<string | null>(null);
  const [chosen, setChosen] = useState<SlotOption | null>(null);
  const [reason, setReason] = useState("");
  // Bumped after any staff write so the grid behind the modal is re-read from
  // SQLite rather than trusted: a slot taken elsewhere must disappear.
  const [slotsEpoch, setSlotsEpoch] = useState(0);

  const options = useStaffQuery("booking-options", (signal) => staffApi.appointments({}, signal));
  const mutation = useStaffMutation(() => {
    setSlotsEpoch((value) => value + 1);
    onCreated();
  });

  async function searchPatients() {
    const needle = patientQuery.trim();
    if (!needle) return;
    try {
      const result = await staffApi.searchPatients(needle);
      setPatientResults(result.patients);
    } catch {
      setPatientResults([]);
    }
  }

  async function loadSlots() {
    setSlotsLoading(true);
    setSlotsError(null);
    setChosen(null);
    try {
      const result = await staffApi.slots(day, providerId, appointmentType);
      setSlots(result.slots);
      setSlotsLoaded(true);
    } catch (err) {
      setSlotsError(err instanceof Error ? err.message : "Could not load availability.");
      setSlots([]);
    } finally {
      setSlotsLoading(false);
    }
  }

  // A staff write elsewhere (or this modal's own retry) invalidates the grid:
  // re-read it for the same day so the receptionist never books a taken time.
  useEffect(() => {
    if (!slotsLoaded || slotsEpoch === 0) return;
    void loadSlots();
    // Only on an epoch bump; loadSlots reads the current filters when it runs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slotsEpoch]);

  const types = options.data?.appointment_types ?? [];
  const providers = options.data?.providers ?? [];

  return (
    <div className="sd-drawer-layer" role="dialog" aria-modal="true" aria-label="New appointment">
      <button type="button" className="sd-scrim sd-scrim--visible" aria-label="Close" onClick={onClose} />
      <div className="sd-modal">
        <header className="sd-drawer__header">
          <div>
            <p className="sd-drawer__eyebrow">New booking</p>
            <h2 className="sd-drawer__title">Create an appointment</h2>
          </div>
          <Button variant="ghost" iconOnly size="sm" onClick={onClose} aria-label="Close" icon={<X size={16} strokeWidth={1.75} />} />
        </header>

        <div className="sd-drawer__body">
          {mutation.error ? <ErrorState message={mutation.error} /> : null}

          <section className="sd-step">
            <div className="sd-step__head">
              <span className="sd-step__num">1</span>
              <h3 className="sd-step__title">Patient</h3>
            </div>
            {patient ? (
              <div className="sd-chipline">
                <span className="sd-chip">
                  <span className="sd-chip__name">{patient.full_name}</span>
                  <span className="sd-chip__meta">{patient.phone}</span>
                </span>
                <Button size="sm" variant="ghost" onClick={() => setPatient(null)}>
                  Change
                </Button>
              </div>
            ) : (
              <>
                <div className="sd-inline-row">
                  <span className="sd-search sd-search--sm sd-search--grow">
                    <Search size={14} strokeWidth={1.75} aria-hidden="true" className="sd-search__icon" />
                    <input
                      className="sd-search__input"
                      type="search"
                      placeholder="Search by name or phone"
                      value={patientQuery}
                      onChange={(event) => setPatientQuery(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") void searchPatients();
                      }}
                    />
                  </span>
                  <Button onClick={() => void searchPatients()} disabled={!patientQuery.trim()}>
                    Search
                  </Button>
                </div>
                {patientResults.length > 0 ? (
                  <ul className="sd-list sd-list--boxed">
                    {patientResults.map((result) => (
                      <li key={result.id}>
                        <button
                          type="button"
                          className="sd-list__row sd-list__row--button"
                          onClick={() => {
                            setPatient(result);
                            setPatientResults([]);
                          }}
                        >
                          <div className="sd-list__who">
                            <span className="sd-list__name">{result.full_name}</span>
                            <span className="sd-list__meta">{result.phone}</span>
                          </div>
                        </button>
                      </li>
                    ))}
                  </ul>
                ) : null}
              </>
            )}
          </section>

          <section className="sd-step">
            <div className="sd-step__head">
              <span className="sd-step__num">2</span>
              <h3 className="sd-step__title">Visit</h3>
            </div>
            <div className="sd-inline-row">
              <label className="field sd-inline-row__grow">
                <span className="sd-field__label">Visit type</span>
                <select
                  className="input"
                  value={appointmentType}
                  onChange={(event) => setAppointmentType(event.target.value)}
                >
                  {(types.length > 0
                    ? types
                    : [{ id: 0, code: "checkup", name: "Checkup", duration_minutes: 30, is_emergency: false }]
                  ).map((type) => (
                    <option key={type.code} value={type.code}>
                      {type.name} ({type.duration_minutes} min)
                    </option>
                  ))}
                </select>
              </label>
              <label className="field sd-inline-row__grow">
                <span className="sd-field__label">Dentist</span>
                <select
                  className="input"
                  value={providerId ?? ""}
                  onChange={(event) => setProviderId(event.target.value ? Number(event.target.value) : null)}
                >
                  <option value="">Any available</option>
                  {providers.map((provider) => (
                    <option key={provider.id} value={provider.id}>
                      {provider.full_name}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          </section>

          <section className="sd-step">
            <div className="sd-step__head">
              <span className="sd-step__num">3</span>
              <h3 className="sd-step__title">Time</h3>
            </div>
            <div className="sd-inline-row">
              <label className="field sd-inline-row__grow">
                <span className="sd-field__label">Day</span>
                <input
                  className="input"
                  type="date"
                  value={day}
                  onChange={(event) => {
                    setDay(event.target.value);
                    setSlotsLoaded(false);
                    setSlots([]);
                    setChosen(null);
                  }}
                />
              </label>
              <Button onClick={() => void loadSlots()} icon={<CalendarDays size={15} strokeWidth={1.75} />}>
                Show open times
              </Button>
            </div>
            {slotsLoading ? <p className="sd-hint">Checking availability…</p> : null}
            {slotsError ? <ErrorState message={slotsError} onRetry={() => void loadSlots()} /> : null}
            {!slotsLoading && !slotsError ? (
              !slotsLoaded ? (
                <p className="sd-hint">Choose a day and load the genuinely free slots.</p>
              ) : slots.length === 0 ? (
                <p className="sd-hint">No open times for this day. Try another day or dentist.</p>
              ) : (
                <div className="sd-slots">
                  {slots.map((slot) => (
                    <button
                      key={`${slot.provider_id}-${slot.starts_at}`}
                      type="button"
                      className={`sd-slot${chosen?.stable_slot_id === slot.stable_slot_id ? " sd-slot--chosen" : ""}`}
                      onClick={() => setChosen(slot)}
                      aria-pressed={chosen?.stable_slot_id === slot.stable_slot_id}
                    >
                      <span className="sd-slot__time">{slot.time_label}</span>
                      <span className="sd-slot__provider">{slot.provider_name}</span>
                    </button>
                  ))}
                </div>
              )
            ) : null}
          </section>

          <label className="field">
            <span className="sd-field__label">Reason / notes (optional)</span>
            <input
              className="input"
              type="text"
              maxLength={500}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
        </div>

        <footer className="sd-modal__footer">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            disabled={!patient || !chosen || mutation.pending}
            onClick={() => {
              if (!patient || !chosen) return;
              void mutation.run(() =>
                staffApi.createAppointment({
                  patient_id: patient.id,
                  provider_id: chosen.provider_id,
                  starts_at: chosen.starts_at,
                  appointment_type: appointmentType,
                  reason: reason.trim() || undefined,
                }),
              );
            }}
          >
            {mutation.pending ? "Booking…" : "Book appointment"}
          </Button>
        </footer>
      </div>
    </div>
  );
}
