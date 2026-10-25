/**
 * `/staff` and `/staff/login` -- the protected dashboard entry point.
 *
 * Routing is decided from the server's answer, not from anything in the URL:
 *
 *   checking        -> a quiet splash (session lookup happens on every load)
 *   anonymous       -> <StaffLoginPage />
 *   authenticated   -> <StaffDashboard />
 *
 * `loginRoute` only affects the splash copy. Because the authenticated case
 * always renders the dashboard, a signed-in user who visits `/staff/login`
 * lands on the dashboard rather than a form they do not need.
 */

import { StaffAuthProvider, useStaffAuth } from "./StaffAuthContext";
import { StaffDashboard } from "./StaffDashboard";
import { StaffLoginPage } from "./StaffLoginPage";
import "./staff.css";

function StaffGate() {
  const { status } = useStaffAuth();

  if (status === "checking") {
    return (
      <div className="sd-splash" role="status" aria-live="polite">
        <span className="sd-loading__spinner" aria-hidden="true" />
        <p>Checking your session…</p>
      </div>
    );
  }

  if (status === "anonymous") {
    return <StaffLoginPage />;
  }

  return <StaffDashboard />;
}

export function StaffApp() {
  return (
    <StaffAuthProvider>
      <StaffGate />
    </StaffAuthProvider>
  );
}
