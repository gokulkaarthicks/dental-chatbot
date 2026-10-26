/**
 * Patients section: look someone up and see their visit history.
 *
 * One search field drives everything. When results arrive they replace the
 * empty prompt; selecting a patient shows the record in a right-hand detail
 * column so the list stays available for the next caller.
 */

import { useState, type FormEvent } from "react";
import { ChevronRight, Search } from "lucide-react";

import { staffApi, type PatientDetailResponse, type StaffPatientRef } from "../../../api/staff";
import { Button } from "../../../components/Button";
import { fullTimestamp } from "../../../lib/staffFormat";
import { appointmentBadge, EmptyState, ErrorState, SkeletonRows } from "../StaffUi";
import { useStaffQuery } from "../useStaffData";

export function PatientsSection() {
  const [term, setTerm] = useState("");
  const [submitted, setSubmitted] = useState("");
  const [selectedId, setSelectedId] = useState<number | null>(null);

  const search = useStaffQuery(`patient-search:${submitted}`, (signal) =>
    submitted
      ? staffApi.searchPatients(submitted, signal)
      : Promise.resolve({ patients: [] as StaffPatientRef[] }),
  );

  const detail = useStaffQuery(`patient-detail:${selectedId ?? "none"}`, (signal) =>
    selectedId
      ? staffApi.patient(selectedId, signal)
      : Promise.resolve(null as PatientDetailResponse | null),
  );

  function runSearch(event?: FormEvent) {
    event?.preventDefault();
    setSelectedId(null);
    setSubmitted(term.trim());
  }

  const results = search.data?.patients ?? [];
  const hasResults = results.length > 0;

  return (
    <div className="sd-stack">
      <form className="sd-toolbar" onSubmit={runSearch} role="search">
        <div className="sd-search">
          <Search size={15} strokeWidth={1.75} aria-hidden="true" className="sd-search__icon" />
          <input
            className="sd-search__input"
            type="search"
            placeholder="Search by name or phone number"
            aria-label="Search patients"
            value={term}
            onChange={(event) => setTerm(event.target.value)}
          />
        </div>
        <Button type="submit" variant="primary" disabled={!term.trim()}>
          Search
        </Button>
      </form>

      {search.error ? <ErrorState message={search.error} onRetry={search.reload} /> : null}

      <div className={`sd-split${selectedId ? " sd-split--detail" : ""}`}>
        <section className="sd-panel" aria-label="Search results">
          <header className="sd-panel__header">
            <div>
              <h2 className="sd-panel__title">Patients</h2>
              <p className="sd-panel__sub">
                {submitted
                  ? search.loading
                    ? "Searching…"
                    : `${results.length} ${results.length === 1 ? "match" : "matches"} for “${submitted}”`
                  : "Search to find a patient record"}
              </p>
            </div>
          </header>

          {submitted && search.loading ? <SkeletonRows rows={5} label="Searching patients" /> : null}

          {submitted && !search.loading && !search.error && !hasResults ? (
            <EmptyState title="No matching patients" hint="Try a different spelling or the phone number." />
          ) : null}

          {!submitted ? (
            <EmptyState title="No patient selected" hint="Enter a name or phone number above." />
          ) : null}

          {hasResults ? (
            <ul className="sd-list">
              {results.map((patient) => (
                <li key={patient.id}>
                  <button
                    type="button"
                    className={`sd-list__row sd-list__row--button${
                      selectedId === patient.id ? " sd-list__row--selected" : ""
                    }`}
                    onClick={() => setSelectedId(patient.id)}
                    aria-current={selectedId === patient.id ? "true" : undefined}
                  >
                    <div className="sd-list__who">
                      <span className="sd-list__name">{patient.full_name}</span>
                      <span className="sd-list__meta">{patient.phone}</span>
                    </div>
                    <ChevronRight
                      size={15}
                      strokeWidth={1.75}
                      aria-hidden="true"
                      className="sd-list__chev"
                    />
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
        </section>

        {selectedId ? (
          <section className="sd-panel" aria-label="Patient record">
            {detail.loading ? <SkeletonRows rows={6} label="Loading patient" /> : null}
            {detail.error ? <ErrorState message={detail.error} onRetry={detail.reload} /> : null}

            {detail.data ? (
              <>
                <header className="sd-panel__header">
                  <div>
                    <h2 className="sd-panel__title">{detail.data.patient.full_name}</h2>
                    <p className="sd-panel__sub">{detail.data.patient.phone}</p>
                  </div>
                </header>

                <dl className="sd-facts">
                  <div>
                    <dt>Date of birth</dt>
                    <dd>{detail.data.patient.date_of_birth ?? "--"}</dd>
                  </div>
                  <div>
                    <dt>Insurance</dt>
                    <dd>{detail.data.patient.insurance_provider ?? "Self pay"}</dd>
                  </div>
                  {detail.data.patient.insurance_member_id ? (
                    <div>
                      <dt>Member ID</dt>
                      <dd className="sd-mono">{detail.data.patient.insurance_member_id}</dd>
                    </div>
                  ) : null}
                </dl>

                <div className="sd-subhead">
                  <h3 className="sd-subhead__title">Visit history</h3>
                  <span className="sd-subhead__count">
                    {detail.data.appointments.length}{" "}
                    {detail.data.appointments.length === 1 ? "visit" : "visits"}
                  </span>
                </div>

                {detail.data.appointments.length === 0 ? (
                  <EmptyState title="No appointments yet" />
                ) : (
                  <div className="sd-table-wrap">
                    <table className="sd-table">
                      <thead>
                        <tr>
                          <th scope="col">When</th>
                          <th scope="col">Visit</th>
                          <th scope="col">Dentist</th>
                          <th scope="col">Status</th>
                          <th scope="col">Booked</th>
                        </tr>
                      </thead>
                      <tbody>
                        {detail.data.appointments.map((row) => (
                          <tr key={row.id}>
                            <td>
                              {row.day_label}
                              <span className="sd-cell-sub">{row.time_label}</span>
                            </td>
                            <td>{row.appointment_type?.name ?? "Visit"}</td>
                            <td>{row.provider?.full_name ?? "--"}</td>
                            <td>{appointmentBadge(row.status)}</td>
                            <td>
                              <span className="sd-cell-sub">{fullTimestamp(row.created_at)}</span>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </>
            ) : null}
          </section>
        ) : null}
      </div>
    </div>
  );
}
