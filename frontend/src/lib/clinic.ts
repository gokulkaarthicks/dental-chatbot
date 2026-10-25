/**
 * The clinic's name, read from the backend's single configuration source.
 *
 * `GET /api/clinic` serves `ClinicConfig` from `backend/app/clinic.py`. The
 * patient chat and the staff dashboard both read it here, so neither interface
 * carries its own hardcoded branding and the two can never disagree.
 *
 * The fetch is memoised per page load: the name does not change between
 * requests, and a slow or failed request must never block rendering. A neutral
 * fallback is used until it arrives (and if the backend is unreachable).
 */

import { api } from "../api/client";

export const CLINIC_NAME_FALLBACK = "Dental Clinic";

let cached: string | null = null;
let inFlight: Promise<string> | null = null;
const listeners = new Set<(name: string) => void>();

function remember(name: string): string {
  const trimmed = name.trim();
  if (!trimmed) return CLINIC_NAME_FALLBACK;
  cached = trimmed;
  for (const listener of listeners) listener(trimmed);
  return trimmed;
}

/** Fetch (once) and return the configured clinic name. Never throws. */
export async function loadClinicName(signal?: AbortSignal): Promise<string> {
  if (cached) return cached;
  if (!inFlight) {
    inFlight = api
      .clinic(signal)
      .then((clinic) => remember(clinic.name))
      .catch(() => cached ?? CLINIC_NAME_FALLBACK)
      .finally(() => {
        inFlight = null;
      });
  }
  return inFlight;
}

/** The name if it is already known, otherwise the neutral fallback. */
export function cachedClinicName(): string {
  return cached ?? CLINIC_NAME_FALLBACK;
}

/** Observe the name arriving after the first `loadClinicName` call. */
export function onClinicName(listener: (name: string) => void): () => void {
  listeners.add(listener);
  if (cached) listener(cached);
  return () => listeners.delete(listener);
}

/** Seed the cache from a payload that already carried the name (chat session). */
export function seedClinicName(name: string | null | undefined): void {
  if (!name || !name.trim()) return;
  remember(name);
}

/** Two-part branding for a stacked lockup ("Oral-B" / "Dental Care"). */
export function splitClinicName(name: string): { lead: string; rest: string } {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (words.length <= 2) return { lead: name.trim(), rest: "" };
  return { lead: words.slice(0, 2).join(" "), rest: words.slice(2).join(" ") };
}
