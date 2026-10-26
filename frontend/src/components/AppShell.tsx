/**
 * Page chrome for the operations overview: brand, nav, footer, one main slot.
 *
 * The patient chat deliberately does not use this shell -- it has its own
 * full-height layout -- so this exists only for `/overview`.
 */

import type { ReactNode } from "react";
import { CircleDot } from "lucide-react";

import { useClinicName } from "../hooks/useClinicName";

/** App chrome. Always renders so the demo never shows a blank screen. */
export function AppShell({ children }: { children: ReactNode }) {
  // Branding comes from /api/clinic, the same source the patient chat uses.
  const clinicName = useClinicName();
  return (
    <div className="app">
      <header className="app__header">
        <div className="app__brand">
          <span className="app__mark" aria-hidden="true">
            <CircleDot size={16} strokeWidth={2} />
          </span>
          <div>
            <p className="app__eyebrow">AI Receptionist · Operations overview</p>
            <h1 className="app__title">{clinicName}</h1>
          </div>
        </div>
        <nav className="app__nav" aria-label="Documentation links">
          <a href="/">Patient chat</a>
          <a href="/docs" target="_blank" rel="noreferrer">
            API docs
          </a>
          <a href="/openapi.json" target="_blank" rel="noreferrer">
            OpenAPI
          </a>
          <a href="/api/demo/status" target="_blank" rel="noreferrer">
            Demo status
          </a>
        </nav>
      </header>
      <main className="app__main">{children}</main>
      <footer className="app__footer">
        <p>Fictional clinic for demonstration purposes. No real patient data is stored.</p>
      </footer>
    </div>
  );
}
