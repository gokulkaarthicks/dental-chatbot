/**
 * Clinic branding for both interfaces, read from `/api/clinic`.
 *
 * Two levels on purpose: the chat sidebar needs the full profile (hours,
 * address, phone, insurance) while the chrome only needs the name. Both settle
 * on the same configured answer, so neither interface can show different
 * branding for the same clinic.
 */

import { useEffect, useState } from "react";

import { api, type ClinicResponse } from "../api/client";
import { cachedClinicName, loadClinicName, onClinicName } from "../lib/clinic";

/**
 * The clinic name from backend configuration.
 *
 * Renders the cached value immediately when there is one, otherwise a neutral
 * fallback until `/api/clinic` answers; a failed request simply leaves the
 * fallback in place rather than blocking the page.
 */
export function useClinicName(): string {
  const [name, setName] = useState<string>(() => cachedClinicName());

  useEffect(() => {
    let active = true;
    const stop = onClinicName((next) => {
      if (active) setName(next);
    });
    const controller = new AbortController();
    void loadClinicName(controller.signal).then((next) => {
      if (active) setName(next);
    });
    return () => {
      active = false;
      stop();
      controller.abort();
    };
  }, []);

  return name;
}

/**
 * Full clinic configuration (hours, address, phone, insurance).
 *
 * The sidebar is chrome, not data the conversation depends on: a failed request
 * leaves it with the name only rather than blocking the chat.
 */
export function useClinicInfo(): ClinicResponse | null {
  const [clinic, setClinic] = useState<ClinicResponse | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void api
      .clinic(controller.signal)
      .then((payload) => setClinic(payload))
      .catch(() => undefined);
    return () => controller.abort();
  }, []);

  return clinic;
}
