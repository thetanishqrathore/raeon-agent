/**
 * GatewayProvider — owns a single {@link GatewayClient} for the chat surface and
 * keeps it connected.
 *
 * The underlying client (web/src/lib/gatewayClient.ts) does a one-shot connect
 * and never reconnects on its own. This provider wraps it with:
 *   - auto-connect on mount (with the ticket/token auth the client handles),
 *   - exponential-backoff reconnect when the socket drops,
 *   - a React-friendly `state` value for status UI.
 *
 * Consumers read `{ client, state }` via {@link useGateway}. The client instance
 * is stable for the provider's lifetime, so event subscriptions set up in child
 * effects survive reconnects (the client re-uses the same listener registry).
 */

import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { GatewayClient, type ConnectionState } from "@/lib/gatewayClient";

interface GatewayContextValue {
  client: GatewayClient;
  state: ConnectionState;
}

const GatewayContext = createContext<GatewayContextValue | null>(null);

const RECONNECT_BASE_MS = 1_000;
const RECONNECT_MAX_MS = 15_000;

export function GatewayProvider({ children }: { children: ReactNode }) {
  // useState initializer guarantees a single, stable instance per provider.
  const [client] = useState(() => new GatewayClient());
  const [state, setState] = useState<ConnectionState>(client.state);

  useEffect(() => {
    let disposed = false;
    let attempt = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const schedule = () => {
      if (disposed) return;
      if (timer) clearTimeout(timer);
      const delay = Math.min(RECONNECT_BASE_MS * 2 ** attempt, RECONNECT_MAX_MS);
      attempt += 1;
      timer = setTimeout(connect, delay);
    };

    const connect = async () => {
      if (disposed) return;
      try {
        await client.connect();
        attempt = 0; // reset backoff once we're open
      } catch {
        // The client already transitioned to "error"; the onState handler
        // below schedules the retry, so don't double-schedule here.
      }
    };

    const unsub = client.onState((s) => {
      if (disposed) return;
      setState(s);
      if (s === "closed" || s === "error") schedule();
    });

    void connect();

    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
      unsub();
      client.close();
    };
  }, [client]);

  return (
    <GatewayContext.Provider value={{ client, state }}>
      {children}
    </GatewayContext.Provider>
  );
}

export function useGateway(): GatewayContextValue {
  const ctx = useContext(GatewayContext);
  if (!ctx) {
    throw new Error("useGateway must be used within <GatewayProvider>");
  }
  return ctx;
}
