/**
 * History hydration — convert stored REST messages into the chat model.
 *
 * Full-fidelity history comes from REST `GET /api/sessions/:id/messages` (the WS
 * session.history RPC drops tool results). The stored format is OpenAI-style:
 * user / assistant(content + tool_calls) / tool(result, linked by tool_call_id),
 * with `reasoning` carried on assistant rows. We group each turn's
 * assistant+tool rows into a single assistant message whose ordered parts mirror
 * the live transcript (reasoning → tool cards (with results) → text).
 */

import {
  coerceText,
  formatToolResult,
  type ChatMessage,
  type ToolPart,
} from "./model";

export interface StoredToolCall {
  id?: string;
  call_id?: string;
  function?: { name?: string; arguments?: string };
}

export interface StoredMessage {
  role: "user" | "assistant" | "system" | "tool";
  content?: string | null;
  tool_calls?: StoredToolCall[] | string | null;
  tool_name?: string | null;
  tool_call_id?: string | null;
  reasoning?: string | null;
  reasoning_content?: string | null;
  active?: number | boolean | string | null;
  timestamp?: number;
}

let hSeq = 0;
const hid = () => `h${(hSeq++).toString(36)}`;

/** Mirror agent/display.py::build_tool_preview — pick the "which X" label from args. */
export function previewFromArgs(name: string, argsJson?: string): string | undefined {
  if (!argsJson) return undefined;
  let args: Record<string, unknown>;
  try {
    args = JSON.parse(argsJson) as Record<string, unknown>;
  } catch {
    return undefined;
  }
  const str = (k: string) => (typeof args[k] === "string" ? (args[k] as string) : undefined);
  switch (name) {
    case "web_search":
      return str("query");
    case "web_extract": {
      const urls = args.urls;
      return Array.isArray(urls) && urls.length ? String(urls[0]) : str("url");
    }
    case "browser_navigate":
      return str("url");
    case "read_file":
    case "write_file":
    case "edit_file":
      return str("path");
    case "terminal":
      return str("command");
    case "skill_view":
    case "skill_manage":
      return str("name");
    case "delegate_task":
      return str("goal");
    default:
      return undefined;
  }
}

const MAX = 4000;

/**
 * Extract the first complete JSON value from a string via a balanced scan, so
 * trailing prose after the JSON (common in stored tool results) doesn't break
 * parsing. Returns the JSON substring or null.
 */
export function extractJson(s: string): string | null {
  const start = s.search(/[{[]/);
  if (start < 0) return null;
  const open = s[start];
  const close = open === "{" ? "}" : "]";
  let depth = 0;
  let inStr = false;
  let esc = false;
  for (let i = start; i < s.length; i++) {
    const c = s[i];
    if (inStr) {
      if (esc) esc = false;
      else if (c === "\\") esc = true;
      else if (c === '"') inStr = false;
      continue;
    }
    if (c === '"') inStr = true;
    else if (c === open) depth++;
    else if (c === close && --depth === 0) return s.slice(start, i + 1);
  }
  return null;
}

/** Strip the `<untrusted_tool_result …>` wrapper + preamble and summarize. */
export function storedToolResult(content: string | null | undefined): {
  text: string;
  isError: boolean;
} {
  if (!content) return { text: "", isError: false };
  let body = content;
  const tag = body.match(/<untrusted_tool_result[^>]*>/);
  if (tag) {
    body = body.slice(body.indexOf(tag[0]) + tag[0].length);
    // Drop the matching closing tag so it doesn't leak into the raw fallback.
    const close = body.lastIndexOf("</untrusted_tool_result>");
    if (close !== -1) body = body.slice(0, close);
    body = body.trim();
    // Drop the fixed anti-injection preamble paragraph the wrapper prepends
    // (agent/tool_dispatch_helpers.py::_maybe_wrap_untrusted).
    if (body.startsWith("The following content was retrieved from an external source.")) {
      const para = body.indexOf("\n\n");
      if (para !== -1) body = body.slice(para + 2);
    }
  }
  body = body.trim();

  const json = extractJson(body);
  if (json) {
    try {
      return formatToolResult(JSON.parse(json));
    } catch {
      /* not clean JSON — fall through to raw */
    }
  }
  return { text: body.length > MAX ? `${body.slice(0, MAX)}\n…(truncated)` : body, isError: false };
}

function coerceToolCalls(tc: StoredMessage["tool_calls"]): StoredToolCall[] {
  if (Array.isArray(tc)) return tc;
  if (typeof tc === "string") {
    try {
      const parsed = JSON.parse(tc);
      return Array.isArray(parsed) ? (parsed as StoredToolCall[]) : [];
    } catch {
      return [];
    }
  }
  return [];
}

const isInactive = (m: StoredMessage) =>
  m.active === 0 || m.active === false || m.active === "0";

export function restToChatMessages(messages: StoredMessage[]): ChatMessage[] {
  const out: ChatMessage[] = [];
  let current: ChatMessage | null = null;
  const toolIndex = new Map<string, ToolPart>();

  const flush = () => {
    if (current) {
      out.push(current);
      current = null;
    }
  };
  const ensureAssistant = (ts?: number): ChatMessage => {
    if (!current) {
      current = { id: hid(), role: "assistant", parts: [], pending: false, createdAt: ts ?? 0 };
    }
    return current;
  };

  for (const m of messages) {
    if (isInactive(m)) continue;

    if (m.role === "user") {
      flush();
      out.push({
        id: hid(),
        role: "user",
        parts: [{ type: "text", text: coerceText(m.content) }],
        pending: false,
        createdAt: m.timestamp ?? 0,
      });
    } else if (m.role === "system") {
      flush();
      const text = coerceText(m.content);
      if (text) {
        out.push({
          id: hid(),
          role: "system",
          parts: [{ type: "text", text }],
          pending: false,
          createdAt: m.timestamp ?? 0,
        });
      }
    } else if (m.role === "assistant") {
      const a = ensureAssistant(m.timestamp);
      const reasoning = coerceText(m.reasoning ?? m.reasoning_content);
      if (reasoning) a.parts.push({ type: "reasoning", text: reasoning });
      for (const tc of coerceToolCalls(m.tool_calls)) {
        const id = tc.id ?? tc.call_id ?? hid();
        const name = tc.function?.name ?? "tool";
        const part: ToolPart = {
          type: "tool",
          id,
          name,
          context: previewFromArgs(name, tc.function?.arguments),
          status: "done",
        };
        a.parts.push(part);
        toolIndex.set(id, part);
      }
      const text = coerceText(m.content);
      if (text) a.parts.push({ type: "text", text });
    } else if (m.role === "tool") {
      ensureAssistant(m.timestamp);
      const fmt = storedToolResult(m.content);
      const part = m.tool_call_id ? toolIndex.get(m.tool_call_id) : undefined;
      if (part) {
        part.result = fmt.text;
        part.status = fmt.isError ? "error" : "done";
        if (m.tool_name) part.name = part.name === "tool" ? m.tool_name : part.name;
      } else {
        current!.parts.push({
          type: "tool",
          id: m.tool_call_id ?? hid(),
          name: m.tool_name ?? "tool",
          status: fmt.isError ? "error" : "done",
          result: fmt.text,
        });
      }
    }
  }

  flush();
  return out;
}
