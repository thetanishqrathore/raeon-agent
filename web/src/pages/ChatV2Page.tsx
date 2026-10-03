/**
 * ChatV2Page — the native chat surface.
 *
 * Renders the live agent turn (reasoning, tool calls with their scrape/skill
 * context, streamed markdown) over the tui_gateway WebSocket, with a session
 * sidebar and full REST-hydrated history. Mounted at /chatx, parallel to the
 * existing xterm /chat during the build-out.
 *
 * Session identity has two ids: a STORED id (persistent, shown in the sidebar,
 * used for REST history) and a LIVE id (issued by session.create / .resume,
 * used for WS event routing + prompt.submit).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Activity,
  ArrowDown,
  Bug,
  Loader2,
  Newspaper,
  PanelLeftOpen,
  Radar,
  Send,
  Sparkles,
  Square,
  Users,
  type LucideIcon,
} from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { useBelowBreakpoint } from "@nous-research/ui/hooks/use-below-breakpoint";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { setAgentName, useAgentName } from "@/lib/brand";
import type { ConnectionState } from "@/lib/gatewayClient";
import { GatewayProvider, useGateway } from "@/chat/gateway/GatewayProvider";
import {
  createSession,
  interruptSession,
  resumeSession,
  submitPrompt,
} from "@/chat/gateway/protocol";
import { useChat, type RawEvent } from "@/chat/useChat";
import { restToChatMessages, type StoredMessage } from "@/chat/history";
import { MessageView } from "@/chat/components/MessageView";
import { SessionSidebar } from "@/chat/components/SessionSidebar";
import { PromptPanel } from "@/chat/components/PromptPanel";

const CONN_LABEL: Record<ConnectionState, string> = {
  idle: "idle",
  connecting: "connecting…",
  open: "connected",
  closed: "reconnecting…",
  error: "reconnecting…",
};

// Quick-start prompts on the empty state — one per core Hermes workflow.
const SUGGESTIONS: { icon: LucideIcon; title: string; desc: string; prompt: string }[] = [
  {
    icon: Radar,
    title: "Deep research",
    desc: "Multi-source brief with citations",
    prompt:
      "Run deep research on a topic of my choice — ask me for the topic and scope first, then produce a cited brief.",
  },
  {
    icon: Users,
    title: "Find leads",
    desc: "Source + enrich contacts for outreach",
    prompt:
      "Help me source new leads: ask for my ICP criteria, then find and enrich matching contacts into a review sheet.",
  },
  {
    icon: Newspaper,
    title: "Founder feed digest",
    desc: "What matters in today's feed",
    prompt: "Summarize today's founder feed — the top items and why they matter to me.",
  },
  {
    icon: Activity,
    title: "System check",
    desc: "Services, disk, crons, anomalies",
    prompt:
      "Check the VM: service health, disk usage, failed cron runs, and anything unusual in the logs.",
  },
];

/** Scroll distance (px) from the bottom under which we keep auto-following. */
const FOLLOW_THRESHOLD = 80;

function ConnectionDot({ state }: { state: ConnectionState }) {
  const color =
    state === "open"
      ? "bg-green-500"
      : state === "connecting"
        ? "bg-amber-500"
        : "bg-red-500";
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
      <span className="relative flex size-2">
        {state !== "open" && (
          <span className={cn("absolute inline-flex h-full w-full animate-ping rounded-full opacity-60", color)} />
        )}
        <span className={cn("relative inline-flex size-2 rounded-full", color)} />
      </span>
      {CONN_LABEL[state]}
    </span>
  );
}

function RawEventDrawer({ log }: { log: RawEvent[] }) {
  return (
    <div className="flex w-80 shrink-0 flex-col border-l border-border bg-background/60">
      <div className="border-b border-border px-3 py-2 text-xs font-medium text-muted-foreground">
        Raw events ({log.length})
      </div>
      <div className="chat-scroll flex-1 overflow-auto p-2 font-mono text-[10px] leading-tight">
        {log
          .slice()
          .reverse()
          .map((e) => (
            <div key={e.seq} className="mb-1 border-b border-border/30 pb-1">
              <span className="text-primary">{e.type}</span>
              <pre className="whitespace-pre-wrap break-words text-muted-foreground">
                {JSON.stringify(e.payload)}
              </pre>
            </div>
          ))}
      </div>
    </div>
  );
}

function ChatV2Inner() {
  const { client, state } = useGateway();
  const agentName = useAgentName();

  // The app-shell sidebar (which normally feeds the brand store) may be
  // unmounted on mobile — fetch once here so the chat is always branded.
  useEffect(() => {
    api
      .getStatus()
      .then((s) => setAgentName(s.display_name))
      .catch(() => {});
  }, []);
  const [liveId, setLiveId] = useState<string | null>(null);
  const [storedId, setStoredId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [showRaw, setShowRaw] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const [opening, setOpening] = useState(false);
  const [openError, setOpenError] = useState<string | null>(null);
  const {
    messages,
    status,
    busy,
    model,
    prompt,
    rawLog,
    pushUser,
    reset,
    hydrate,
    clearPrompt,
    sendFailed,
  } = useChat(client, liveId);
  const scrollRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const startingRef = useRef(false);
  const prevBusy = useRef(false);
  const prevStateRef = useRef<ConnectionState>("idle");

  // Stick-to-bottom: keep following the stream only while the user is near the
  // bottom; scrolling up detaches, and a floating button re-attaches.
  const [followStream, setFollowStream] = useState(true);
  const followRef = useRef(true);
  const setFollow = useCallback((v: boolean) => {
    followRef.current = v;
    setFollowStream(v);
  }, []);
  const onTranscriptScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el || el.scrollHeight <= el.clientHeight) return;
    const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
    const next = dist < FOLLOW_THRESHOLD;
    if (next !== followRef.current) setFollow(next);
  }, [setFollow]);
  const scrollToBottom = useCallback(() => {
    const el = scrollRef.current;
    if (el && el.scrollHeight > el.clientHeight + 4) {
      el.scrollTo({ top: el.scrollHeight });
      return;
    }
    // Mobile (<768px): the document scrolls instead of the inner pane. Only
    // follow if the viewport is already near the content's end.
    const doc = document.documentElement;
    const dist = doc.scrollHeight - window.scrollY - window.innerHeight;
    if (dist < FOLLOW_THRESHOLD + 60) window.scrollTo({ top: doc.scrollHeight });
  }, []);

  // Session-sidebar visibility: desktop collapse (persisted) + mobile drawer.
  const isMobile = useBelowBreakpoint(1024);
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem("hermes-chat-sidebar-collapsed") === "true";
    } catch {
      return false;
    }
  });
  const [mobileOpen, setMobileOpen] = useState(false);
  const persistCollapsed = useCallback((v: boolean) => {
    setCollapsed(v);
    try {
      localStorage.setItem("hermes-chat-sidebar-collapsed", String(v));
    } catch {
      /* private mode — non-persistent is fine */
    }
  }, []);
  const openSidebar = useCallback(() => {
    if (isMobile) setMobileOpen(true);
    else persistCollapsed(false);
  }, [isMobile, persistCollapsed]);
  const closeSidebar = useCallback(() => {
    if (isMobile) setMobileOpen(false);
    else persistCollapsed(true);
  }, [isMobile, persistCollapsed]);

  const newChat = async () => {
    if (startingRef.current) return;
    startingRef.current = true;
    setOpening(true);
    try {
      const res = await createSession(client);
      reset();
      setLiveId(res.session_id);
      setStoredId(res.stored_session_id ?? null);
      setRefreshKey((k) => k + 1);
      setFollow(true);
      setOpenError(null);
    } catch {
      /* surfaced via connection state */
    } finally {
      startingRef.current = false;
      setOpening(false);
    }
  };

  // `quiet` = transient-reconnect path: keep the current transcript on screen
  // (no reset, no opening spinner) and swap in fresh history when it arrives.
  // Kills the blank-flash + scroll-jump on every WS drop/reconnect cycle.
  const openChat = async (sid: string, force = false, quiet = false) => {
    if ((!force && sid === storedId) || startingRef.current) return;
    startingRef.current = true;
    if (!quiet) setOpening(true);
    try {
      if (!quiet) reset();
      const res = await resumeSession(client, sid);
      setLiveId(res.session_id);
      setStoredId(sid);
      const hist = await api.getSessionMessages(sid);
      hydrate(restToChatMessages(hist.messages as unknown as StoredMessage[]), res.info?.model);
      setRefreshKey((k) => k + 1);
      if (!quiet) setFollow(true);
      setOpenError(null);
    } catch {
      // Keep the stored id so the banner's retry can resume it.
      setStoredId(sid);
      setOpenError("Couldn't open this chat — the session may still be loading.");
    } finally {
      startingRef.current = false;
      setOpening(false);
    }
  };

  // Bootstrap on first connect; re-attach on reconnect (the live session is
  // reaped server-side on disconnect, so resume the stored id + reload history).
  // If the flip-to-open lands while another open is in flight (startingRef),
  // the reattach is deferred: `opening` is in the dep list, so the effect
  // re-runs when that open settles and consumes the pending flag.
  const needsReattachRef = useRef(false);
  useEffect(() => {
    const prev = prevStateRef.current;
    prevStateRef.current = state;
    if (state !== "open") return;
    if (prev !== "open" && prev !== "idle") needsReattachRef.current = true;
    if (startingRef.current) return;
    if (!liveId) {
      needsReattachRef.current = false;
      void newChat();
    } else if (needsReattachRef.current) {
      needsReattachRef.current = false;
      if (storedId) void openChat(storedId, true, true);
      else void newChat();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state, liveId, storedId, opening]);

  // Refresh the sidebar when a turn finishes (new session appears, title updates).
  useEffect(() => {
    if (prevBusy.current && !busy) setRefreshKey((k) => k + 1);
    prevBusy.current = busy;
  }, [busy]);

  // Keep the transcript pinned to the bottom as it streams — but only while
  // the user hasn't scrolled up to read something.
  useEffect(() => {
    if (followRef.current) scrollToBottom();
  }, [messages, status, scrollToBottom]);

  // Auto-grow the composer with its content (capped), reset when cleared.
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "0px";
    ta.style.height = `${Math.min(ta.scrollHeight, 160)}px`;
  }, [input]);

  const sendText = async (raw: string) => {
    const text = raw.trim();
    if (!text || !liveId) return;
    setInput("");
    setFollow(true);
    pushUser(text);
    try {
      await submitPrompt(client, liveId, text);
    } catch (e) {
      sendFailed(`Failed to send: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const send = () => sendText(input);

  const stop = async () => {
    if (!liveId) return;
    try {
      await interruptSession(client, liveId);
    } catch {
      /* ignore */
    }
  };

  return (
    <div className="flex min-h-0 flex-1">
      {isMobile && mobileOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/60 backdrop-blur-sm lg:hidden"
          onClick={() => setMobileOpen(false)}
          aria-hidden
        />
      )}
      <SessionSidebar
        activeId={storedId}
        onSelect={(id) => {
          void openChat(id);
          if (isMobile) setMobileOpen(false);
        }}
        onNew={() => {
          void newChat();
          if (isMobile) setMobileOpen(false);
        }}
        refreshKey={refreshKey}
        collapsed={collapsed}
        mobileOpen={mobileOpen}
        onClose={closeSidebar}
      />

      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <div className="flex items-center gap-3 border-b border-border px-3 py-2">
          {(isMobile || collapsed) && (
            <Button
              ghost
              size="icon"
              onClick={openSidebar}
              aria-label="Show sessions"
              title="Show sessions"
            >
              <PanelLeftOpen className="size-4" />
            </Button>
          )}
          <h1 className="text-sm font-semibold">Chat</h1>
          <ConnectionDot state={state} />
          <div className="ml-auto flex items-center gap-2">
            {model && (
              <span
                className="hidden max-w-48 truncate rounded-full border border-border/60 bg-muted/30 px-2 py-0.5 font-mono text-[10px] text-muted-foreground sm:inline-block"
                title={model}
              >
                {model}
              </span>
            )}
            <Button ghost size="sm" onClick={() => setShowRaw((v) => !v)}>
              <Bug className="size-4" />
              {showRaw ? "Hide" : "Events"}
            </Button>
          </div>
        </div>

        <div className="flex min-h-0 flex-1">
          <div className="flex min-w-0 flex-1 flex-col">
            <div className="relative min-h-0 flex-1">
              <div
                ref={scrollRef}
                onScroll={onTranscriptScroll}
                className="chat-scroll h-full overflow-auto py-4"
              >
                <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 px-2">
                  {opening && (
                    <div className="flex items-center justify-center gap-2 py-8 text-xs text-muted-foreground">
                      <Loader2 className="size-3 animate-spin" />
                      Loading…
                    </div>
                  )}
                  {!opening && openError && (
                    <div className="flex items-center justify-center gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">
                      <span>{openError}</span>
                      {storedId && (
                        <button
                          type="button"
                          onClick={() => void openChat(storedId, true)}
                          className="rounded border border-amber-500/40 px-2 py-0.5 font-medium transition-colors hover:bg-amber-500/20"
                        >
                          Retry
                        </button>
                      )}
                    </div>
                  )}
                  {!opening && messages.length === 0 && (
                    <div className="flex flex-col items-center gap-6 px-4 pt-[12vh] text-center">
                      <div className="relative">
                        <div
                          className="absolute -inset-8 rounded-full opacity-70 blur-2xl"
                          style={{ background: "var(--warm-glow)" }}
                        />
                        <div className="relative flex size-14 items-center justify-center rounded-2xl border border-border bg-card shadow-lg">
                          <Sparkles className="size-6 text-primary" />
                        </div>
                      </div>
                      <div className="space-y-1.5">
                        <h2 className="text-xl font-semibold tracking-tight">
                          {state === "open"
                            ? `What should ${agentName} dig into?`
                            : `Connecting to ${agentName}…`}
                        </h2>
                        <p className="mx-auto max-w-md text-sm text-muted-foreground">
                          Research runs, lead sourcing, ops checks — with live reasoning,
                          tools, and sources as it works.
                        </p>
                      </div>
                      {state === "open" && liveId && (
                        <div className="grid w-full max-w-xl gap-2 sm:grid-cols-2">
                          {SUGGESTIONS.map((s) => (
                            <button
                              key={s.title}
                              type="button"
                              onClick={() => void sendText(s.prompt)}
                              className="group flex items-start gap-2.5 rounded-xl border border-border/70 bg-card/60 px-3.5 py-3 text-left transition hover:border-primary/40 hover:bg-card"
                            >
                              <s.icon className="mt-0.5 size-4 shrink-0 text-muted-foreground transition-colors group-hover:text-primary" />
                              <span className="min-w-0">
                                <span className="block text-sm font-medium">{s.title}</span>
                                <span className="block text-xs text-muted-foreground">
                                  {s.desc}
                                </span>
                              </span>
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                  {messages.map((m) => (
                    <MessageView key={m.id} msg={m} />
                  ))}
                  {busy && status && (
                    <div className="flex items-center gap-2 text-xs">
                      <span className="relative flex size-2 shrink-0">
                        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary/40" />
                        <span className="relative inline-flex size-2 rounded-full bg-primary/80" />
                      </span>
                      <span className="chat-shimmer min-w-0 truncate">{status}</span>
                    </div>
                  )}
                </div>
              </div>

              {!followStream && (
                <button
                  type="button"
                  onClick={() => {
                    setFollow(true);
                    scrollToBottom();
                  }}
                  className="absolute bottom-3 left-1/2 z-10 flex -translate-x-1/2 items-center gap-1.5 rounded-full border border-border bg-card px-3 py-1.5 text-xs text-muted-foreground shadow-lg transition-colors hover:text-foreground"
                >
                  <ArrowDown className="size-3.5" />
                  Latest
                </button>
              )}
            </div>

            <div className="border-t border-border p-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] max-md:sticky max-md:bottom-0 max-md:z-10 max-md:bg-background/95 max-md:backdrop-blur">
              {prompt ? (
                <PromptPanel
                  prompt={prompt}
                  client={client}
                  sessionId={liveId}
                  onResolved={clearPrompt}
                />
              ) : (
                <div className="mx-auto w-full max-w-3xl">
                  <div className="flex items-end gap-1.5 rounded-2xl border border-border bg-card/70 px-3 py-1.5 shadow-sm transition-colors focus-within:border-primary/50">
                    <textarea
                      ref={taRef}
                      value={input}
                      onChange={(e) => setInput(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && !e.shiftKey) {
                          e.preventDefault();
                          void send();
                        }
                      }}
                      rows={1}
                      placeholder={liveId ? `Message ${agentName}…` : "Starting session…"}
                      disabled={!liveId}
                      className="max-h-40 min-h-[38px] flex-1 resize-none bg-transparent py-2 text-sm outline-none placeholder:text-muted-foreground/70 disabled:opacity-50"
                    />
                    {busy ? (
                      <Button
                        size="icon"
                        destructive
                        className="mb-0.5 shrink-0 rounded-full"
                        onClick={() => void stop()}
                        title="Stop generating"
                        aria-label="Stop generating"
                      >
                        <Square className="size-4" />
                      </Button>
                    ) : (
                      <Button
                        size="icon"
                        className="mb-0.5 shrink-0 rounded-full"
                        onClick={() => void send()}
                        disabled={!input.trim() || !liveId}
                        title="Send"
                        aria-label="Send"
                      >
                        <Send className="size-4" />
                      </Button>
                    )}
                  </div>
                  <div className="mt-1.5 hidden px-1 text-[10px] text-muted-foreground/60 md:block">
                    Enter to send · Shift+Enter for a new line
                  </div>
                </div>
              )}
            </div>
          </div>

          {showRaw && <RawEventDrawer log={rawLog} />}
        </div>
      </div>
    </div>
  );
}

export default function ChatV2Page() {
  return (
    <GatewayProvider>
      <ChatV2Inner />
    </GatewayProvider>
  );
}
