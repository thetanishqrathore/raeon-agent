/**
 * Agent display-name store ("Hermes" by default, e.g. "Helix" per deployment).
 *
 * The name comes from `/api/status` (`display_name`, backed by config
 * `dashboard.display_name`). A tiny external store lets any component react to
 * it without prop-drilling: callers that fetch status pass the field to
 * {@link setAgentName}; components render via {@link useAgentName}.
 */

import { useSyncExternalStore } from "react";

const DEFAULT_NAME = "Hermes";

let agentName = DEFAULT_NAME;
const listeners = new Set<() => void>();

export function setAgentName(name?: string | null): void {
  const next = (name ?? "").trim() || DEFAULT_NAME;
  if (next === agentName) return;
  agentName = next;
  listeners.forEach((fn) => fn());
}

export function getAgentName(): string {
  return agentName;
}

export function useAgentName(): string {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => agentName,
  );
}
