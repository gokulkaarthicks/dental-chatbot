import { api, type ClinicResponse, type DemoStatusResponse, type HealthResponse } from "../api/client";
import { AppShell } from "../components/AppShell";
import { Card } from "../components/Card";
import { HoursList } from "../components/HoursList";
import { ErrorBlock, LoadingBlock } from "../components/Loading";
import { StatusPill } from "../components/StatusPill";
import { Stat, StatGrid } from "../components/Stat";
import { useApi } from "../hooks/useApi";
import { useResetGeneration } from "../hooks/useResetGeneration";

const healthLoader = (signal: AbortSignal) => api.health(signal);
const clinicLoader = (signal: AbortSignal) => api.clinic(signal);
const statusLoader = (signal: AbortSignal) => api.demoStatus(signal);

function shortGeneration(value: string | null | undefined): string {
  if (!value) return "none";
  return value.length > 8 ? value.slice(0, 8) : value;
}

function formatSlot(value: string | null): string {
  if (!value) return "none";
  const [date, time] = value.split("T");
  return `${date} at ${time}`;
}

export function OverviewPage() {
  const health = useApi<HealthResponse>(healthLoader);
  const clinic = useApi<ClinicResponse>(clinicLoader);
  const status = useApi<DemoStatusResponse>(statusLoader);

  const generation = useResetGeneration(status.data?.reset_generation);

  return (
    <AppShell>
      <section className="hero">
        <div>
          <p className="hero__eyebrow">Foundation status</p>
          <h2 className="hero__title">The clinic is wired up and ready for the receptionist.</h2>
          <p className="hero__lede">
            Phase 1 delivers the database schema, reproducible demo fixtures, and the three
            foundation endpoints. Conversations, booking, and the staff dashboard arrive next.
          </p>
        </div>
        <div className="hero__badges">
          {health.error ? (
            <StatusPill tone="error">API unreachable</StatusPill>
          ) : health.loading ? (
            <StatusPill tone="neutral">Checking API…</StatusPill>
          ) : (
            <StatusPill tone={health.data?.status === "ok" ? "ok" : "warn"}>
              API {health.data?.status ?? "unknown"}
            </StatusPill>
          )}
          {status.data && (
            <StatusPill tone={status.data.seeded ? "ok" : "warn"}>
              {status.data.seeded ? "Fixtures loaded" : "No fixtures"}
            </StatusPill>
          )}
        </div>
      </section>

      {generation.changed && (
        <div className="banner banner--warn" role="status">
          The demo database was reset. Any earlier conversation, staff session, or booking
          proposal from this browser is no longer valid.
        </div>
      )}

      <div className="grid grid--two">
        <Card
          title="Health"
          subtitle="GET /api/health"
          actions={
            <button type="button" className="button button--ghost" onClick={health.reload}>
              Refresh
            </button>
          }
        >
          {health.loading && <LoadingBlock label="Contacting API…" />}
          {health.error && <ErrorBlock message={health.error} onRetry={health.reload} />}
          {health.data && (
            <dl className="key-values">
              <div>
                <dt>Status</dt>
                <dd>{health.data.status}</dd>
              </div>
              <div>
                <dt>Database</dt>
                <dd>{health.data.database}</dd>
              </div>
              <div>
                <dt>API version</dt>
                <dd>{health.data.version}</dd>
              </div>
              <div>
                <dt>Schema version</dt>
                <dd>{health.data.schema_version ?? "--"}</dd>
              </div>
              <div>
                <dt>Reset generation</dt>
                <dd title={health.data.reset_generation ?? ""}>
                  <code>{shortGeneration(health.data.reset_generation)}</code>
                </dd>
              </div>
            </dl>
          )}
        </Card>

        <Card
          title="Clinic"
          subtitle="GET /api/clinic"
          actions={
            <button type="button" className="button button--ghost" onClick={clinic.reload}>
              Refresh
            </button>
          }
        >
          {clinic.loading && <LoadingBlock label="Loading clinic profile…" />}
          {clinic.error && <ErrorBlock message={clinic.error} onRetry={clinic.reload} />}
          {clinic.data && (
            <div className="clinic">
              <address className="clinic__address">
                <strong>{clinic.data.name}</strong>
                <span>{clinic.data.address.line1}</span>
                <span>{clinic.data.address.line2}</span>
                <span>
                  {clinic.data.address.city}, {clinic.data.address.state}{" "}
                  {clinic.data.address.postal_code}
                </span>
                <span>{clinic.data.phone}</span>
              </address>
              <div className="clinic__hours">
                <h3>Business hours</h3>
                <HoursList hours={clinic.data.business_hours} today={status.data?.clinic_today_label} />
              </div>
              <p className="clinic__meta">
                {clinic.data.timezone} · {clinic.data.slot_minutes}-minute slots
              </p>
            </div>
          )}
        </Card>
      </div>

      <Card
        title="Demo data"
        subtitle="GET /api/demo/status"
        actions={
          <div className="card__meta">
            <span>Reset #{status.data?.reset_count ?? "--"}</span>
            <button type="button" className="button button--ghost" onClick={status.reload}>
              Refresh
            </button>
          </div>
        }
      >
        {status.loading && <LoadingBlock label="Counting fixtures…" />}
        {status.error && <ErrorBlock message={status.error} onRetry={status.reload} />}
        {status.data && (
          <>
            <StatGrid>
              <Stat label="Dentists" value={status.data.counts.providers} />
              <Stat label="Patients" value={status.data.counts.patients} />
              <Stat label="Family groups" value={status.data.counts.family_groups} />
              <Stat label="Upcoming visits" value={status.data.counts.upcoming_appointments} />
              <Stat label="Open slots" value={status.data.counts.available_slots} />
              <Stat label="Occupied slots" value={status.data.counts.occupied_slots} />
            </StatGrid>
            <div className="status-footer">
              <span>
                Next opening: <strong>{formatSlot(status.data.next_available_slot)}</strong>
              </span>
              <span>
                Horizon: {status.data.horizon_days} days from {status.data.clinic_today}
              </span>
            </div>
          </>
        )}
      </Card>

      <Card title="What's next" tone="muted">
        <ol className="next-steps">
          <li>DeepSeek-backed conversations with caller identification.</li>
          <li>Booking proposals, slot claims, and confirmation flows.</li>
          <li>Staff dashboard for proposals, alerts, and pending requests.</li>
        </ol>
      </Card>
    </AppShell>
  );
}
