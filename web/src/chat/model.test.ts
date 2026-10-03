import { describe, it, expect } from "vitest";
import type { GatewayEvent } from "@/lib/gatewayClient";
import {
  chatReducer,
  initialChatState,
  type ChatState,
  type ToolPart,
} from "./model";

const ev = (type: string, payload?: unknown, session_id = "s1"): GatewayEvent => ({
  type,
  session_id,
  payload,
});

function run(state: ChatState, events: GatewayEvent[]): ChatState {
  return events.reduce((s, e) => chatReducer(s, { kind: "event", ev: e }), state);
}

describe("chatReducer", () => {
  it("adds a user message and marks busy", () => {
    const s = chatReducer(initialChatState, { kind: "user", text: "hi" });
    expect(s.messages).toHaveLength(1);
    expect(s.messages[0].role).toBe("user");
    expect(s.messages[0].parts).toEqual([{ type: "text", text: "hi" }]);
    expect(s.busy).toBe(true);
  });

  it("streams assistant text across deltas", () => {
    const s = run(initialChatState, [
      ev("message.start"),
      ev("message.delta", { text: "hel" }),
      ev("message.delta", { text: "lo" }),
    ]);
    expect(s.messages).toHaveLength(1);
    const m = s.messages[0];
    expect(m.role).toBe("assistant");
    expect(m.pending).toBe(true);
    expect(m.parts).toEqual([{ type: "text", text: "hello" }]);
  });

  it("preserves arrival order: reasoning → tool → text", () => {
    const s = run(initialChatState, [
      ev("message.start"),
      ev("reasoning.delta", { text: "thinking…" }),
      ev("tool.start", { tool_id: "t1", name: "web_search", context: "tokyo population" }),
      ev("message.delta", { text: "Tokyo has ~14M." }),
    ]);
    const parts = s.messages[0].parts;
    expect(parts.map((p) => p.type)).toEqual(["reasoning", "tool", "text"]);
    expect((parts[1] as ToolPart).context).toBe("tokyo population");
  });

  it("completes a tool with result + duration + error flag", () => {
    let s = run(initialChatState, [
      ev("message.start"),
      ev("tool.start", { tool_id: "t1", name: "web_search", context: "q" }),
    ]);
    s = run(s, [
      ev("tool.complete", { tool_id: "t1", summary: "5 results", duration_s: 1.23 }),
    ]);
    const tool = s.messages[0].parts.find((p): p is ToolPart => p.type === "tool")!;
    expect(tool.status).toBe("done");
    expect(tool.result).toBe("5 results");
    expect(tool.durationS).toBeCloseTo(1.23);

    const s2 = run(
      run(initialChatState, [ev("message.start"), ev("tool.start", { tool_id: "x", name: "terminal" })]),
      [ev("tool.complete", { tool_id: "x", error: true, result: "boom" })],
    );
    const t2 = s2.messages[0].parts.find((p): p is ToolPart => p.type === "tool")!;
    expect(t2.status).toBe("error");
  });

  it("attaches inline diffs to the tool", () => {
    const s = run(initialChatState, [
      ev("message.start"),
      ev("tool.start", { tool_id: "e", name: "edit_file" }),
      ev("tool.complete", { tool_id: "e", inline_diff: "+added\n-removed" }),
    ]);
    const tool = s.messages[0].parts.find((p): p is ToolPart => p.type === "tool")!;
    expect(tool.inlineDiff).toContain("+added");
  });

  it("summarizes web_search results instead of dumping JSON", () => {
    const s = run(initialChatState, [
      ev("message.start"),
      ev("tool.start", { tool_id: "w", name: "web_search", context: "tokyo" }),
      ev("tool.complete", {
        tool_id: "w",
        name: "web_search",
        duration_s: 1.4,
        result: {
          success: true,
          data: {
            web: [
              { title: "Tokyo Population 2026", url: "https://example.com/a" },
              { title: "Macrotrends", url: "https://example.com/b" },
            ],
          },
        },
      }),
    ]);
    const tool = s.messages[0].parts.find((p): p is ToolPart => p.type === "tool")!;
    expect(tool.status).toBe("done");
    expect(tool.result).toContain("2 result(s)");
    expect(tool.result).toContain("Tokyo Population 2026 — https://example.com/a");
    expect(tool.result).not.toContain("success");
  });

  it("treats result.success === false as an error", () => {
    const s = run(initialChatState, [
      ev("message.start"),
      ev("tool.start", { tool_id: "f", name: "web_extract" }),
      ev("tool.complete", { tool_id: "f", result: { success: false, error: "blocked" } }),
    ]);
    const tool = s.messages[0].parts.find((p): p is ToolPart => p.type === "tool")!;
    expect(tool.status).toBe("error");
    expect(tool.result).toContain("blocked");
  });

  it("finalizes the assistant turn on message.complete", () => {
    const s = run(initialChatState, [
      ev("message.start"),
      ev("message.delta", { text: "done" }),
      ev("message.complete", { text: "done" }),
    ]);
    expect(s.messages[0].pending).toBe(false);
    expect(s.busy).toBe(false);
    // No duplicate text from the final payload.
    expect(s.messages[0].parts).toEqual([{ type: "text", text: "done" }]);
  });

  it("uses final text when nothing streamed", () => {
    const s = run(initialChatState, [
      ev("message.start"),
      ev("message.complete", { text: "the answer" }),
    ]);
    expect(s.messages[0].parts).toEqual([{ type: "text", text: "the answer" }]);
  });

  it("sets a transient status from thinking/status/browser events", () => {
    expect(chatReducer(initialChatState, { kind: "event", ev: ev("thinking.delta", { text: "🔍 searching" }) }).status).toBe("🔍 searching");
    expect(chatReducer(initialChatState, { kind: "event", ev: ev("browser.progress", { url: "example.com" }) }).status).toBe("Browsing example.com");
  });

  it("records model + running from session.info", () => {
    const s = chatReducer(initialChatState, { kind: "event", ev: ev("session.info", { model: "gpt-5.5", running: true }) });
    expect(s.model).toBe("gpt-5.5");
    expect(s.busy).toBe(true);
  });

  it("pushes a system message on error", () => {
    const s = chatReducer(initialChatState, { kind: "event", ev: ev("error", { message: "kaboom" }) });
    expect(s.messages[0].role).toBe("system");
    expect(s.messages[0].parts[0]).toEqual({ type: "text", text: "⚠️ kaboom" });
  });

  it("returns the same state reference for unhandled events (no needless re-render)", () => {
    const before = run(initialChatState, [ev("message.start")]);
    const after = chatReducer(before, { kind: "event", ev: ev("skin.changed", { name: "x" }) });
    expect(after).toBe(before);
  });

  it("clears text on reset but keeps the model", () => {
    let s = run(initialChatState, [ev("session.info", { model: "m1" }), ev("message.start"), ev("message.delta", { text: "x" })]);
    s = chatReducer(s, { kind: "reset" });
    expect(s.messages).toHaveLength(0);
    expect(s.model).toBe("m1");
  });

  it("raises a clarify prompt and clears it on response/complete", () => {
    const s = chatReducer(initialChatState, {
      kind: "event",
      ev: ev("clarify.request", { request_id: "r1", question: "Which one?", choices: ["a", "b"] }),
    });
    expect(s.prompt).toEqual({
      kind: "clarify",
      requestId: "r1",
      question: "Which one?",
      choices: ["a", "b"],
    });
    expect(chatReducer(s, { kind: "clearPrompt" }).prompt).toBeNull();
    expect(chatReducer(s, { kind: "event", ev: ev("message.complete", {}) }).prompt).toBeNull();
  });

  it("raises approval / sudo / secret prompts with their fields", () => {
    const ap = chatReducer(initialChatState, {
      kind: "event",
      ev: ev("approval.request", { command: "rm -rf /tmp/x", description: "delete", allow_permanent: true }),
    });
    expect(ap.prompt).toMatchObject({ kind: "approval", command: "rm -rf /tmp/x", allowPermanent: true });

    const su = chatReducer(initialChatState, {
      kind: "event",
      ev: ev("sudo.request", { request_id: "s1", prompt: "sudo pw" }),
    });
    expect(su.prompt).toMatchObject({ kind: "sudo", requestId: "s1", promptText: "sudo pw" });

    const se = chatReducer(initialChatState, {
      kind: "event",
      ev: ev("secret.request", { request_id: "x1", env_var: "API_KEY", prompt: "enter key" }),
    });
    expect(se.prompt).toMatchObject({ kind: "secret", requestId: "x1", envVar: "API_KEY" });
  });

  it("builds a subagent spawn-tree with steps and completion", () => {
    const s = run(initialChatState, [
      ev("message.start"),
      ev("subagent.start", { subagent_id: "sa1", goal: "enrich leads", model: "gpt-5.5", depth: 1 }),
      ev("subagent.tool", { subagent_id: "sa1", tool_name: "web_search", tool_preview: "acme corp" }),
      ev("subagent.tool", { subagent_id: "sa1", tool_name: "web_extract", tool_preview: "https://acme.com" }),
      ev("subagent.complete", { subagent_id: "sa1", summary: "found 3 contacts", duration_seconds: 4.2 }),
    ]);
    const sub = s.messages[0].parts.find((p) => p.type === "subagent");
    expect(sub?.type).toBe("subagent");
    if (sub?.type === "subagent") {
      expect(sub.goal).toBe("enrich leads");
      expect(sub.model).toBe("gpt-5.5");
      expect(sub.status).toBe("done");
      expect(sub.steps).toEqual([
        { tool: "web_search", preview: "acme corp" },
        { tool: "web_extract", preview: "https://acme.com" },
      ]);
      expect(sub.summary).toBe("found 3 contacts");
      expect(sub.durationS).toBeCloseTo(4.2);
    }
  });
});
