import { StrictMode, useEffect } from "react";
import { createRoot } from "react-dom/client";

import { cachedClinicName, loadClinicName, onClinicName } from "./lib/clinic";
import { ChatPage } from "./pages/ChatPage";
import { OverviewPage } from "./pages/OverviewPage";
import { StaffApp } from "./pages/staff/StaffApp";
import "./styles/global.css";
import "./styles/chat.css";
import "./styles/cards.css";

const container = document.getElementById("root");
if (!container) {
  throw new Error("Root element #root was not found in index.html");
}

/**
 * Tiny path switch, no router dependency.
 *
 *   /                 patient chat (Phase 2, the primary experience)
 *   /overview         the Phase 1 operations overview, kept working
 *   /staff            staff dashboard (Phase 4) -- renders the login screen
 *                     unless the server confirms a session
 *   /staff/login      the same gate; a signed-in user is sent to the dashboard
 *
 * Anything else (including /docs, /api/...) is served by the backend or nginx.
 * The staff guard is not enforced here: that is the API's job, and this switch
 * only decides which component to mount.
 */
const path = window.location.pathname.replace(/\/+$/, "") || "/";

function render() {
  if (path === "/overview") return <OverviewPage />;
  if (path === "/staff" || path.startsWith("/staff/")) return <StaffApp />;
  return <ChatPage />;
}

const TITLE_FALLBACK = "Front Desk";

/**
 * Keep the document title on the same branding as the visible chrome.
 *
 * The name comes from the shared clinic configuration (`/api/clinic`), never a
 * literal, and is fetched once per page load through the memoised helper; the
 * staff pages build their own clinic query too, and both settle on one answer.
 * Until it arrives the neutral fallback is used, never another clinic's name.
 */
function useBrandedTitle() {
  useEffect(() => {
    let active = true;
    const apply = (name: string) => {
      if (active && name) document.title = `${name} · ${TITLE_FALLBACK}`;
    };
    apply(cachedClinicName());
    const stop = onClinicName(apply);
    const controller = new AbortController();
    void loadClinicName(controller.signal).then(apply);
    return () => {
      active = false;
      stop();
      controller.abort();
    };
  }, []);
}

function Root() {
  useBrandedTitle();
  return render();
}

createRoot(container).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
