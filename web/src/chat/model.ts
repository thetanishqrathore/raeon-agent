/**
 * Chat domain model + a pure event-folding reducer.
 *
 * This is the "ported logic" half of the hybrid renderer: the transcript-shaping
 * transitions are lifted from the desktop app's use-message-stream.ts
 * (handleGatewayEvent), stripped of all Electron/desktop side-effects (pet,
 * haptics, native notifications, react-query, background-process syncing). What
 * remains is the part that's genuinely tricky to get right: how each gateway
 * event mutates the message/part tree.
 *
 * The reducer is pure and session-agnostic (the hook filters events by session
 * before dispatching), which makes it straightforward to unit-test.
 */

import type { GatewayEvent } from "@/lib/gatewayClient";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type ToolStatus = "running" | "done" | "error";

export interface TextPart {
  type: "text";
  text: string;
}

export interface ReasoningPart {
  type: "reasoning";
  text: string;
}

export interface ToolPart {
  type: "tool";
  id: string;
  name: string;
  /** "which site / which skill" — from build_tool_preview (tool.start.context). */
  context?: string;
  argsText?: string;
  status: ToolStatus;
  result?: string;
  durationS?: number;
  inlineDiff?: string;
}

export interface SubagentStep {
  tool: string;
  preview?: string;
}

export interface SubagentPart {
  type: "subagent";
  id: string;
  goal?: string;
  model?: string;
  depth?: number;
  status: "running" | "done" | "error" | "interrupted";
  steps: SubagentStep[];
  summary?: string;
  durationS?: number;
}

export type Part = TextPart | ReasoningPart | ToolPart | SubagentPart;

export type Role = "user" | "assistant" | "system";

export interface ChatMessage {
  id: string;
  role: Role;
  parts: Part[];
  /** True while the assistant turn is still streaming. */
  pending: boolean;
  createdAt: number;
}

/** An interactive request that BLOCKS the turn until answered via a *.respond RPC. */
export interface PendingPrompt {
  kind: "clarify" | "approval" | "sudo" | "secret";
  /** "" for approval (resolved by session_id, not request_id). */
  requestId: string;
  question?: string;
  choices?: string[];
  command?: string;
  description?: string;
  envVar?: string;
  promptText?: string;
  allowPermanent?: boolean;
}

export interface ChatState {
  messages: ChatMessage[];
  /** Transient current-activity line (thinking/status/browse), cleared as output flows. */
  status: string;
  model: string;
  busy: boolean;
  /** Active blocking prompt (clarify/approval/sudo/secret), or null. */
  prompt: PendingPrompt | null;
}

export const initialChatState: ChatState = {
  messages: [],
  status: "",
  model: "",
  busy: false,
  prompt: null,
};

export type ChatAction =
  | { kind: "event"; ev: GatewayEvent }
  | { kind: "user"; text: string }
  | { kind: "hydrate"; messages: ChatMessage[]; model?: string }
  | { kind: "reset" }
  | { kind: "clearPrompt" }
  /** prompt.submit failed after the optimistic user push — un-stick the turn. */
  | { kind: "sendFailed"; text: string };

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

let idSeq = 0;
const nextId = () => `c${Date.now().toString(36)}${(idSeq++).toString(36)}`;

const asStr = (v: unknown): string | undefined =>
  typeof v === "string" ? v : undefined;
const asNum = (v: unknown): number | undefined =>
  typeof v === "number" ? v : undefined;
const asBool = (v: unknown): boolean | undefined =>
  typeof v === "boolean" ? v : undefined;

/** Coerce a gateway text-ish value (string | {text|content|…} | array) to a string. */
export function coerceText(value: unknown, depth = 0): string {
  if (typeof value === "string") return value;
  if (value == null || depth > 2) return "";
  if (Array.isArray(value)) return value.map((v) => coerceText(v, depth + 1)).join("");
  if (typeof value === "object") {
    const row = value as Record<string, unknown>;
    const nested = coerceText(
      row.text ?? row.output_text ?? row.content ?? row.message,
      depth + 1,
    );
    if (nested) return nested;
    try {
      return JSON.stringify(value);
    } catch {
      return "";
    }
  }
  return String(value);
}

const MAX_RESULT_CHARS = 4000;
const truncate = (s: string) =>
  s.length > MAX_RESULT_CHARS ? `${s.slice(0, MAX_RESULT_CHARS)}\n…(truncated)` : s;

function safeJson(v: unknown): string {
  try {
    return JSON.stringify(v, null, 1);
  } catch {
    return "";
  }
}

/**
 * Turn a tool's raw result into a compact, human-friendly summary for the tool
 * card. Tool results come back as structured objects (e.g. web_search →
 * {success, data:{web:[{title,url,…}]}}); raw JSON is an ugly fallback, so we
 * special-case the common shapes. `success: false` signals an error.
 */
export function formatToolResult(
  raw: unknown,
  summary?: string,
): { text: string; isError: boolean } {
  if (summary && summary.trim()) return { text: summary.trim(), isError: false };
  if (raw == null) return { text: "", isError: false };
  if (typeof raw === "string") return { text: truncate(raw), isError: false };
  if (typeof raw !== "object") return { text: String(raw), isError: false };

  const r = raw as Record<string, unknown>;
  const isError = asBool(r.success) === false;
  if (isError) {
    const msg =
      asStr(r.error) || coerceText(r.message) || coerceText(r.data) || safeJson(r);
    return { text: truncate(msg), isError: true };
  }

  const data =
    r.data && typeof r.data === "object" ? (r.data as Record<string, unknown>) : r;

  const web = data.web ?? data.results;
  if (Array.isArray(web) && web.length) {
    const lines = web.slice(0, 8).map((item) => {
      const it = (item ?? {}) as Record<string, unknown>;
      const url = asStr(it.url);
      return `• ${asStr(it.title) ?? ""}${url ? ` — ${url}` : ""}`;
    });
    return { text: `${web.length} result(s)\n${lines.join("\n")}`, isError: false };
  }

  const textual = coerceText(data.content ?? data.text ?? data.output);
  if (textual) return { text: truncate(textual), isError: false };

  return { text: truncate(safeJson(r)), isError: false };
}

function newAssistant(): ChatMessage {
  return { id: nextId(), role: "assistant", parts: [], pending: true, createdAt: Date.now() };
}

/**
 * Return a new messages array with the current streaming assistant message
 * transformed, creating a fresh pending assistant message if the last message
 * isn't one. `transform` receives a message whose `parts` array is a fresh copy
 * it may mutate.
 */
function withAssistant(
  messages: ChatMessage[],
  transform: (m: ChatMessage) => void,
): ChatMessage[] {
  const last = messages[messages.length - 1];
  if (last && last.role === "assistant" && last.pending) {
    const draft: ChatMessage = { ...last, parts: [...last.parts] };
    transform(draft);
    return [...messages.slice(0, -1), draft];
  }
  const fresh = newAssistant();
  transform(fresh);
  return [...messages, fresh];
}

function appendText(parts: Part[], text: string) {
  const last = parts[parts.length - 1];
  if (last && last.type === "text") last.text += text;
  else parts.push({ type: "text", text });
}

function appendReasoning(parts: Part[], text: string) {
  const last = parts[parts.length - 1];
  if (last && last.type === "reasoning") last.text += text;
  else parts.push({ type: "reasoning", text });
}

function toolKey(p: Record<string, unknown>): string {
  return asStr(p.tool_id) ?? asStr(p.tool_call_id) ?? asStr(p.name) ?? "tool";
}

// ---------------------------------------------------------------------------
// Reducer
// ---------------------------------------------------------------------------

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.kind) {
    case "reset":
      return { ...initialChatState, model: state.model };

    case "hydrate":
      return {
        ...state,
        messages: action.messages,
        model: action.model ?? state.model,
        status: "",
        busy: false,
        prompt: null,
      };

    case "clearPrompt":
      return state.prompt ? { ...state, prompt: null } : state;

    case "sendFailed":
      // The optimistic "user" push set busy=true; no message.complete will
      // ever arrive for a prompt that never reached the server.
      return {
        ...state,
        busy: false,
        status: "",
        messages: [
          ...state.messages,
          {
            id: nextId(),
            role: "system",
            parts: [{ type: "text", text: `⚠️ ${action.text}` }],
            pending: false,
            createdAt: Date.now(),
          },
        ],
      };

    case "user":
      return {
        ...state,
        busy: true,
        status: "",
        messages: [
          ...state.messages,
          {
            id: nextId(),
            role: "user",
            parts: [{ type: "text", text: action.text }],
            pending: false,
            createdAt: Date.now(),
          },
        ],
      };

    case "event":
      return applyEvent(state, action.ev);
  }
}

function applyEvent(state: ChatState, ev: GatewayEvent): ChatState {
  const p = (ev.payload ?? {}) as Record<string, unknown>;

  switch (ev.type) {
    case "session.info": {
      const model = asStr(p.model);
      const running = asBool(p.running);
      if (model === undefined && running === undefined) return state;
      return {
        ...state,
        model: model ?? state.model,
        busy: running ?? state.busy,
      };
    }

    case "message.start": {
      const last = state.messages[state.messages.length - 1];
      const alreadyOpen = last && last.role === "assistant" && last.pending;
      return {
        ...state,
        busy: true,
        status: "",
        messages: alreadyOpen ? state.messages : [...state.messages, newAssistant()],
      };
    }

    case "message.delta": {
      const text = coerceText(p.text);
      if (!text) return state;
      return {
        ...state,
        status: "",
        messages: withAssistant(state.messages, (m) => appendText(m.parts, text)),
      };
    }

    case "reasoning.delta":
    case "reasoning.available": {
      const text = coerceText(p.text);
      if (!text) return state;
      return {
        ...state,
        messages: withAssistant(state.messages, (m) => appendReasoning(m.parts, text)),
      };
    }

    case "thinking.delta":
    case "status.update": {
      // Transient activity line (kawaii spinner status / lifecycle notices).
      return { ...state, status: coerceText(p.text) };
    }

    case "browser.progress": {
      const label = asStr(p.url) ?? asStr(p.status) ?? coerceText(p.text);
      return label ? { ...state, status: `Browsing ${label}` } : state;
    }

    case "tool.start":
    case "tool.progress":
    case "tool.generating": {
      const id = toolKey(p);
      const name = asStr(p.name) ?? "tool";
      const context = asStr(p.context) ?? asStr(p.preview);
      const argsText = asStr(p.args_text);
      return {
        ...state,
        status: "",
        messages: withAssistant(state.messages, (m) => {
          const existing = m.parts.find(
            (part): part is ToolPart => part.type === "tool" && part.id === id,
          );
          if (existing) {
            existing.name = name;
            if (context) existing.context = context;
            if (argsText) existing.argsText = argsText;
          } else {
            m.parts.push({ type: "tool", id, name, context, argsText, status: "running" });
          }
        }),
      };
    }

    case "tool.complete": {
      const id = toolKey(p);
      const fmt = formatToolResult(p.result, asStr(p.summary));
      const isError = p.error === true || asBool(p.is_error) === true || fmt.isError;
      const result = fmt.text;
      const durationS = asNum(p.duration_s);
      const inlineDiff = asStr(p.inline_diff);
      let found = false;
      const messages = state.messages.map((m) => {
        if (!m.parts.some((part) => part.type === "tool" && part.id === id)) return m;
        found = true;
        return {
          ...m,
          parts: m.parts.map((part) =>
            part.type === "tool" && part.id === id
              ? {
                  ...part,
                  status: (isError ? "error" : "done") as ToolStatus,
                  result: result || part.result,
                  durationS: durationS ?? part.durationS,
                  inlineDiff: inlineDiff?.trim() ? inlineDiff : part.inlineDiff,
                }
              : part,
          ),
        };
      });
      // tool.complete with no matching start: attach a completed tool to the turn.
      if (!found) {
        return {
          ...state,
          messages: withAssistant(state.messages, (m) =>
            m.parts.push({
              type: "tool",
              id,
              name: asStr(p.name) ?? "tool",
              status: isError ? "error" : "done",
              result,
              durationS,
              inlineDiff: inlineDiff?.trim() ? inlineDiff : undefined,
            }),
          ),
        };
      }
      return { ...state, messages };
    }

    case "message.complete": {
      const finalText = coerceText(p.text) || coerceText(p.rendered);
      const messages = state.messages.map((m) => {
        if (!(m.role === "assistant" && m.pending)) return m;
        const hasText = m.parts.some((part) => part.type === "text" && part.text.length > 0);
        const parts = [...m.parts];
        if (!hasText && finalText) parts.push({ type: "text", text: finalText });
        return { ...m, parts, pending: false };
      });
      return { ...state, messages, status: "", busy: false, prompt: null };
    }

    case "error": {
      const msg = asStr(p.message) ?? coerceText(p.error) ?? "unknown error";
      return {
        ...state,
        busy: false,
        status: "",
        // A turn that died can't answer its blocking prompt — drop it so the
        // PromptPanel doesn't wedge the composer.
        prompt: null,
        messages: [
          ...state.messages,
          {
            id: nextId(),
            role: "system",
            parts: [{ type: "text", text: `⚠️ ${msg}` }],
            pending: false,
            createdAt: Date.now(),
          },
        ],
      };
    }

    case "subagent.start":
    case "subagent.spawn_requested": {
      const id = asStr(p.subagent_id) ?? asStr(p.goal) ?? "sub";
      return {
        ...state,
        status: "",
        messages: withAssistant(state.messages, (m) => {
          let sp = m.parts.find(
            (x): x is SubagentPart => x.type === "subagent" && x.id === id,
          );
          if (!sp) {
            sp = { type: "subagent", id, status: "running", steps: [] };
            m.parts.push(sp);
          }
          sp.goal = sp.goal ?? asStr(p.goal);
          sp.model = sp.model ?? asStr(p.model);
          sp.depth = sp.depth ?? asNum(p.depth);
        }),
      };
    }

    case "subagent.tool": {
      const id = asStr(p.subagent_id) ?? "sub";
      const tool = asStr(p.tool_name) ?? "tool";
      const preview = asStr(p.tool_preview) ?? asStr(p.text);
      return {
        ...state,
        messages: withAssistant(state.messages, (m) => {
          let sp = m.parts.find(
            (x): x is SubagentPart => x.type === "subagent" && x.id === id,
          );
          if (!sp) {
            sp = { type: "subagent", id, status: "running", steps: [] };
            m.parts.push(sp);
          }
          sp.steps = [...sp.steps, { tool, preview }];
        }),
      };
    }

    case "subagent.complete":
    case "subagent.interrupt": {
      const id = asStr(p.subagent_id) ?? "sub";
      const done = ev.type === "subagent.complete";
      return {
        ...state,
        messages: withAssistant(state.messages, (m) => {
          const sp = m.parts.find(
            (x): x is SubagentPart => x.type === "subagent" && x.id === id,
          );
          if (!sp) return;
          sp.status = done ? "done" : "interrupted";
          if (done) {
            sp.summary = asStr(p.summary) ?? sp.summary;
            sp.durationS = asNum(p.duration_seconds) ?? sp.durationS;
          }
        }),
      };
    }

    case "clarify.request": {
      const choices = Array.isArray(p.choices)
        ? (p.choices as unknown[]).filter((c): c is string => typeof c === "string")
        : undefined;
      return {
        ...state,
        prompt: {
          kind: "clarify",
          requestId: asStr(p.request_id) ?? "",
          question: coerceText(p.question),
          choices: choices && choices.length ? choices : undefined,
        },
      };
    }

    case "approval.request":
      return {
        ...state,
        prompt: {
          kind: "approval",
          requestId: "",
          command: coerceText(p.command),
          description: asStr(p.description),
          allowPermanent: asBool(p.allow_permanent) ?? true,
        },
      };

    case "sudo.request":
      return {
        ...state,
        prompt: {
          kind: "sudo",
          requestId: asStr(p.request_id) ?? "",
          promptText: asStr(p.prompt),
        },
      };

    case "secret.request":
      return {
        ...state,
        prompt: {
          kind: "secret",
          requestId: asStr(p.request_id) ?? "",
          envVar: asStr(p.env_var),
          promptText: asStr(p.prompt),
        },
      };

    default:
      // Lifecycle/meta/subagent events + terminal.read (auto-answered in the
      // hook) — no transcript change.
      return state;
  }
}
