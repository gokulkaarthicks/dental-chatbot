/**
 * Reset-generation tracking.
 *
 * Patient-session handling lives in `lib/sessionStore` + `useChat`, which
 * compare the stored generation before trusting a session token; this hook
 * exposes the same comparison to the overview screen.
 */
import { useEffect, useState } from "react";

import { sessionStore } from "../lib/sessionStore";

export type GenerationStatus = {
  /** True when the generation differs from the one this browser last saw. */
  changed: boolean;
  current: string | null;
  previous: string | null;
};

export function useResetGeneration(current: string | null | undefined): GenerationStatus {
  const [previous, setPrevious] = useState<string | null>(() => sessionStore.generation());

  useEffect(() => {
    if (!current) return;
    const stored = sessionStore.generation();
    if (stored !== current) {
      setPrevious(stored);
      sessionStore.setGeneration(current);
    }
  }, [current]);

  return {
    changed: Boolean(previous && current && previous !== current),
    current: current ?? null,
    previous,
  };
}
