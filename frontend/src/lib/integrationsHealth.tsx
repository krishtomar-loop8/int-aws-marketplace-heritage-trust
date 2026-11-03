// App-wide integration health tracker. Polls GET /integrations so the sidebar
// can show a pulsing alert dot when a connector has been down for ≥ 2 consecutive
// checks, and the Integrations tab can render live status + the downtime log
// without each mount re-fetching. The scheduled probe updates the data hourly;
// the client poll is gentle (60s) so a recovery/outage surfaces within a minute.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import type { ReactNode } from "react";
import { getIntegrationsHealth } from "../api";
import { MOCK_INTEGRATIONS_HEALTH } from "../mock";
import type { IntegrationsHealth } from "../types";

const POLL_MS = 60_000;

const EMPTY: IntegrationsHealth = { integrations: [], log: [], alerting: false };

interface IntegrationsHealthValue {
  health: IntegrationsHealth;
  loading: boolean;
  refresh: () => void;
  alerting: boolean;
}

const Ctx = createContext<IntegrationsHealthValue | null>(null);

export function IntegrationsHealthProvider({
  apiBase,
  token,
  mock,
  children,
}: {
  apiBase: string;
  token?: string;
  mock: boolean;
  children: ReactNode;
}) {
  const [health, setHealth] = useState<IntegrationsHealth>(EMPTY);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    if (mock) {
      setHealth(MOCK_INTEGRATIONS_HEALTH);
      setLoading(false);
      return;
    }
    if (!token) return;
    // getIntegrationsHealth never throws — it returns an empty snapshot on failure.
    const data = await getIntegrationsHealth(apiBase, token);
    setHealth(data);
    setLoading(false);
  }, [apiBase, token, mock]);

  useEffect(() => {
    refresh();
    const id = window.setInterval(refresh, POLL_MS);
    return () => window.clearInterval(id);
  }, [refresh]);

  const value = useMemo<IntegrationsHealthValue>(
    () => ({ health, loading, refresh, alerting: health.alerting }),
    [health, loading, refresh],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useIntegrationsHealth(): IntegrationsHealthValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useIntegrationsHealth must be used within an IntegrationsHealthProvider");
  return v;
}
