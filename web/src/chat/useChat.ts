/**
 * useChat — binds the live gateway event stream to the {@link chatReducer}.
 *
 * Subscribes once to every event, filters to the active session (lifecycle
 * events carry no session id and are always applied), and dispatches into the
 * pure reducer. Also keeps a capped raw-event log for the debug drawer.
 */

import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import type { GatewayClient, GatewayEvent } from "@/lib/gatewayClient";
import { respondTerminalRead } from "./gateway/protocol";
import {
  chatReducer,
  initialChatState,
  type ChatMessage,
} from "./model";

export interface RawEvent {
  seq: number;
  type: string;
  payload: unknown;
}

// High-frequency streaming events that are safe to coalesce within one
// animation frame: their reducers only append text, so merging consecutive
// same-type deltas is semantics-preserving.
const DELTA_EVENTS = new Set(["message.delta", "reasoning.delta", "thinking.delta"]);

export function useChat(client: GatewayClient, sessionId: string | null) {
  const [state, dispatch] = useReducer(chatReducer, initialChatState);
  const [rawLog, setRawLog] = useState<RawEvent[]>([]);
  const sessionRef = useRef(sessionId);
  const seqRef = useRef(0);

  useEffect(() => {
    sessionRef.current = sessionId;
  }, [sessionId]);

  useEffect(() => {
    // rAF-coalesced delta buffer: during token streaming, events arrive far
    // faster than the display refreshes. Buffering deltas and flushing once
    // per frame turns N reducer runs + N Markdown re-parses per frame into 1,
    // which is the difference between smooth and janky on long answers.
    let pendingDeltas: GatewayEvent[] = [];
    let pendingRaw: RawEvent[] = [];
    let rafId: number | null = null;

    const flush = () => {
      rafId = null;
      if (pendingDeltas.length) {
        // Merge consecutive same-type deltas into one event (pure text append).
        const merged: GatewayEvent[] = [];
        for (const ev of pendingDeltas) {
          const prev = merged[merged.length - 1];
          const prevText = (prev?.payload as { text?: string } | undefined)?.text;
          const curText = (ev.payload as { text?: string } | undefined)?.text;
          if (prev && prev.type === ev.type && typeof prevText === "string" && typeof curText === "string") {
            prev.payload = { ...(prev.payload as object), text: prevText + curText };
          } else {
            merged.push({ ...ev, payload: { ...(ev.payload as object) } });
          }
        }
        pendingDeltas = [];
        for (const ev of merged) dispatch({ kind: "event", ev });
      }
      if (pendingRaw.length) {
        const batch = pendingRaw;
        pendingRaw = [];
        setRawLog((l) => [...l, ...batch].slice(-500));
      }
    };

    const unsub = client.onAny((ev: GatewayEvent) => {
      // Lifecycle/meta events have no session_id and apply globally; session
      // events only apply to the active conversation.
      if (ev.session_id && ev.session_id !== sessionRef.current) return;
      // The web client has no terminal pane to read — auto-answer empty so the
      // agent's read_terminal call doesn't hang the turn (~300s block).
      if (ev.type === "terminal.read.request") {
        const reqId = (ev.payload as { request_id?: string } | undefined)?.request_id ?? "";
        // Best-effort: if the socket drops before the response lands, the
        // server reaps the request with the session — swallow the rejection
        // so it doesn't surface as an unhandled promise error.
        respondTerminalRead(client, reqId, "").catch(() => {});
      }
      pendingRaw.push({ seq: ++seqRef.current, type: ev.type, payload: ev.payload });
      if (DELTA_EVENTS.has(ev.type)) {
        pendingDeltas.push(ev);
        if (rafId == null) rafId = requestAnimationFrame(flush);
        return;
      }
      // Non-delta event: flush buffered deltas first so ordering is preserved
      // (e.g. message.complete must land after its deltas), then apply.
      if (rafId != null) {
        cancelAnimationFrame(rafId);
      }
      flush();
      dispatch({ kind: "event", ev });
    });
    return () => {
      if (rafId != null) cancelAnimationFrame(rafId);
      unsub();
    };
  }, [client]);

  const pushUser = useCallback((text: string) => dispatch({ kind: "user", text }), []);
  const reset = useCallback(() => {
    dispatch({ kind: "reset" });
    setRawLog([]);
  }, []);
  const hydrate = useCallback(
    (messages: ChatMessage[], model?: string) => dispatch({ kind: "hydrate", messages, model }),
    [],
  );
  const clearPrompt = useCallback(() => dispatch({ kind: "clearPrompt" }), []);
  const sendFailed = useCallback(
    (text: string) => dispatch({ kind: "sendFailed", text }),
    [],
  );

  return {
    messages: state.messages,
    status: state.status,
    model: state.model,
    busy: state.busy,
    prompt: state.prompt,
    rawLog,
    pushUser,
    reset,
    hydrate,
    clearPrompt,
    sendFailed,
  };
}
