/**
 * Dashboard chrome: sidebar, header, and the active section.
 *
 * Navigation is deliberately lightweight -- the app has no router dependency and
 * this is a single-screen tool. The active view is kept in component state and
 * mirrored into the URL hash so a refresh keeps you where you were and the back
 * button works between sections.
 */

import { useCallback, useEffect, useState, type ReactNode } from "react";
import {
  BellRing,
  CalendarDays,
  CircleDot,
  Inbox,
  LayoutGrid,
  MessageSquare,
  Users,
  type LucideIcon,
} from "lucide-react";

import { Button } from "../../components/Button";
import { splitClinicName } from "../../lib/clinic";
import { useClinicName } from "../../hooks/useClinicName";
import { useStaffAuth } from "./StaffAuthContext";
import { useStaffSync } from "./useStaffData";
import { AppointmentsSection } from "./sections/AppointmentsSection";
import { OverviewSection } from "./sections/OverviewSection";
import { RequestsSection } from "./sections/RequestsSection";
import { AlertsSection } from "./sections/AlertsSection";
import { PatientsSection } from "./sections/PatientsSection";

export type StaffView = "overview" | "appointments" | "patients" | "requests" | "alerts";

const VIEWS: { id: StaffView; label: string; icon: LucideIcon }[] = [
  { id: "overview", label: "Overview", icon: LayoutGrid },
  { id: "appointments", label: "Appointments", icon: CalendarDays },
  { id: "patients", label: "Patients", icon: Users },
  { id: "requests", label: "Requests", icon: Inbox },
  { id: "alerts", label: "Alerts", icon: BellRing },
];

function readViewFromHash(): StaffView {
  const raw = window.location.hash.replace(/^#\/?/, "").split("?")[0];
  const match = VIEWS.find((view) => view.id === raw);
  return match ? match.id : "overview";
}

/** "just now" / "2 min ago" for the header sync stamp. */
function formatSyncedAt(at: Date): string {
  const seconds = Math.max(0, Math.round((Date.now() - at.getTime()) / 1000));
  if (seconds < 20) return "just now";
  if (seconds < 90) return "a minute ago";
  return `${Math.round(seconds / 60)} min ago`;
}

export function StaffDashboard({ initialView }: { initialView?: StaffView }) {
  const { user, logout } = useStaffAuth();
  const clinicName = useClinicName();
  const brand = splitClinicName(clinicName);
  const [view, setView] = useState<StaffView>(initialView ?? readViewFromHash());
  const [signingOut, setSigningOut] = useState(false);

  // Remount the active section to force a fresh read. Used when the tab
  // regains focus and on the light polling interval, so a booking made in the
  // patient chat appears without a manual refresh. Sections already reload
  // after their own mutations and after any successful staff write.
  const [dataEpoch, setDataEpoch] = useState(0);
  const [lastSyncedAt, setLastSyncedAt] = useState<Date | null>(null);
  const refreshActiveView = useCallback(() => {
    setLastSyncedAt(new Date());
    setDataEpoch((value) => value + 1);
  }, []);
  useStaffSync(refreshActiveView);

  // Keep the hash in step with the selection so reloads and history behave.
  useEffect(() => {
    const next = `#/${view}`;
    if (window.location.hash !== next) {
      window.history.replaceState(null, "", next);
    }
  }, [view]);

  function navigate(next: StaffView) {
    setView(next);
  }

  async function onLogout() {
    setSigningOut(true);
    await logout();
    window.location.replace("/staff/login");
  }

  const active = VIEWS.find((item) => item.id === view) ?? VIEWS[0];

  let content: ReactNode;
  switch (view) {
    case "appointments":
      content = <AppointmentsSection />;
      break;
    case "patients":
      content = <PatientsSection />;
      break;
    case "requests":
      content = <RequestsSection />;
      break;
    case "alerts":
      content = <AlertsSection />;
      break;
    default:
      content = <OverviewSection onNavigate={navigate} />;
  }

  return (
    <div className="sd-shell">
      <aside className="sd-sidebar" aria-label="Dashboard navigation">
        <div className="sd-sidebar__brand">
          <span className="sd-sidebar__mark" aria-hidden="true">
            <CircleDot size={16} strokeWidth={2} />
          </span>
          <div className="sd-sidebar__identity">
            <p className="sd-sidebar__name">{brand.lead}</p>
            {brand.rest ? <p className="sd-sidebar__sub">{brand.rest}</p> : null}
          </div>
        </div>

        <nav className="sd-nav">
          {VIEWS.map((item) => {
            const Icon = item.icon;
            return (
              <button
                key={item.id}
                type="button"
                className={`sd-nav__item${item.id === view ? " sd-nav__item--active" : ""}`}
                aria-current={item.id === view ? "page" : undefined}
                onClick={() => navigate(item.id)}
              >
                <Icon size={16} strokeWidth={1.75} aria-hidden="true" />
                <span>{item.label}</span>
              </button>
            );
          })}
        </nav>

        <div className="sd-sidebar__footer">
          <a className="sd-sidebar__link" href="/">
            <MessageSquare size={14} strokeWidth={1.75} aria-hidden="true" />
            Patient chat
          </a>
          <a className="sd-sidebar__link" href="/docs" target="_blank" rel="noreferrer">
            API docs
          </a>
        </div>
      </aside>

      <div className="sd-main">
        <header className="sd-header">
          <div className="sd-header__left">
            <h1 className="sd-header__title">{active.label}</h1>
            {/* The list refreshes itself (poll + focus); say so briefly rather
                than letting numbers change with no explanation. */}
            {lastSyncedAt ? (
              <span className="sd-header__sync" role="status">
                Synced {formatSyncedAt(lastSyncedAt)}
              </span>
            ) : null}
          </div>

          <div className="sd-header__right">
            <div className="sd-user">
              <div className="sd-user__text">
                <span className="sd-user__name">{user?.display_name ?? "Staff"}</span>
                <span className="sd-user__role">
                  {user?.role === "clinic_manager" ? "Clinic manager" : "Receptionist"}
                </span>
              </div>
              <span className="sd-user__avatar" aria-hidden="true">
                {(user?.display_name ?? "S").slice(0, 1).toUpperCase()}
              </span>
            </div>
            <Button variant="ghost" size="sm" onClick={onLogout} disabled={signingOut}>
              {signingOut ? "Signing out…" : "Sign out"}
            </Button>
          </div>
        </header>

        <main className="sd-content" key={`${view}-${dataEpoch}`}>
          {content}
        </main>

        <footer className="sd-footer">
          <p>
            Fictional clinic for demonstration purposes. Changes here update the same database the
            patient chat uses.
          </p>
        </footer>
      </div>
    </div>
  );
}
