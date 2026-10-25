import { useEffect, useState } from "react";

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
