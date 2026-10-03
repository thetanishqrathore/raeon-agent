/**
 * Typed helpers and payload shapes for the tui_gateway JSON-RPC protocol.
 *
 * RPC method names and payloads mirror tui_gateway/server.py. Keeping the
 * contract here (rather than inline) lets the chat surface evolve without
 * re-deriving the wire format, and documents exactly what each event carries.
 */

import type { GatewayClient } from "@/lib/gatewayClient";

// ---------------------------------------------------------------------------
// RPC helpers
// ---------------------------------------------------------------------------

export interface CreateSessionResult {
  /** Live session id used for prompt.submit and event routing. */
  session_id: string;
  /** Persistent key used to resume the conversation later. */
  stored_session_id?: string;
}

export interface CreateSessionOptions {
  cwd?: string;
  model?: string;
  provider?: string;
  reasoning_effort?: string;
  title?: string;
  /** Marks where the session originated; defaults to "web". */
  source?: string;
}

/** Create a fresh live session. Must happen before prompt.submit. */
export function createSession(
  client: GatewayClient,
  opts: CreateSessionOptions = {},
): Promise<CreateSessionResult> {
  return client.request<CreateSessionResult>("session.create", {
    source: "web",
    ...opts,
  });
}

/** Submit a user turn. Returns once the server reports it is streaming. */
export async function submitPrompt(
  client: GatewayClient,
  sessionId: string,
  text: string,
): Promise<void> {
  await client.request("prompt.submit", { session_id: sessionId, text });
}

export interface ResumeResult {
  /** New live session id to use for events + prompt.submit. */
  session_id: string;
  /** The stored id that was resumed. */
  resumed?: string;
  info?: { model?: string };
}

/**
 * Re-attach to a stored session. Returns a NEW live session_id (the WS event
 * routing id) — use it for prompt.submit and event filtering. Note: the resume
 * payload's messages drop tool results, so hydrate the transcript from REST
 * (GET /api/sessions/:storedId/messages) instead.
 */
export function resumeSession(
  client: GatewayClient,
  storedId: string,
): Promise<ResumeResult> {
  return client.request<ResumeResult>("session.resume", { session_id: storedId });
}

/** Interrupt the in-flight turn for a session. */
export async function interruptSession(
  client: GatewayClient,
  sessionId: string,
): Promise<void> {
  await client.request("session.interrupt", { session_id: sessionId });
}

// ---------------------------------------------------------------------------
// Interactive prompt responses — a pending request BLOCKS the turn until one
// of these lands (or the turn is interrupted). Param shapes mirror
// tui_gateway/server.py respond handlers.
// ---------------------------------------------------------------------------

export type ApprovalChoice = "once" | "session" | "always" | "deny";

export function respondClarify(client: GatewayClient, requestId: string, answer: string) {
  return client.request("clarify.respond", { request_id: requestId, answer });
}

export function respondSudo(client: GatewayClient, requestId: string, password: string) {
  return client.request("sudo.respond", { request_id: requestId, password });
}

export function respondSecret(client: GatewayClient, requestId: string, value: string) {
  return client.request("secret.respond", { request_id: requestId, value });
}

export function respondTerminalRead(client: GatewayClient, requestId: string, text: string) {
  return client.request("terminal.read.respond", { request_id: requestId, text });
}

/** Approval is resolved by session id (it carries no request_id). */
export function respondApproval(
  client: GatewayClient,
  sessionId: string,
  choice: ApprovalChoice,
) {
  return client.request("approval.respond", { session_id: sessionId, choice });
}

// ---------------------------------------------------------------------------
// Event payload shapes (best-effort; fields are optional and defensive)
// ---------------------------------------------------------------------------

export interface MessageDeltaPayload {
  text?: string;
  /** Terminal-ANSI pre-render; ignored by the web UI in favour of `text`. */
  rendered?: string;
}

export interface ReasoningDeltaPayload {
  text?: string;
  verbose?: boolean;
}

export interface ThinkingDeltaPayload {
  text?: string;
}

export interface ToolStartPayload {
  tool_id?: string;
  name?: string;
  /** The "which site / which skill" string from build_tool_preview. */
  context?: string;
  args_text?: string;
}

export interface ToolCompletePayload {
  tool_id?: string;
  name?: string;
  result?: string;
  summary?: string;
  duration_s?: number;
  is_error?: boolean;
}

export interface BrowserProgressPayload {
  url?: string;
  status?: string;
  text?: string;
}

export interface StatusUpdatePayload {
  kind?: string;
  text?: string;
}

export interface ErrorPayload {
  message?: string;
  code?: number;
}
