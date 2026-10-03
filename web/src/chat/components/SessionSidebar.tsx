/**
 * SessionSidebar — ChatGPT-style session list backed by the REST session API.
 * Lists stored sessions (newest first) grouped by recency, with a local search
 * filter; supports new / switch / rename / delete. Selection passes the STORED
 * id up; the page resumes it over WS + hydrates history from REST.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Loader2,
  MessageSquarePlus,
  PanelLeftClose,
  Pencil,
  Search,
  Trash2,
  X,
} from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { api, getManagementProfile, type SessionInfo } from "@/lib/api";
import { cn } from "@/lib/utils";

function ago(ts?: number): string {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  return `${Math.floor(h / 24)}d`;
}

/** Bucket a last-active unix timestamp (seconds) for the group headers. */
function bucketOf(ts?: number): string {
  if (!ts) return "Older";
  const now = new Date();
  const d = new Date(ts * 1000);
  const startOfDay = (x: Date) =>
    new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const dayMs = 86_400_000;
  const diffDays = Math.floor((startOfDay(now) - startOfDay(d)) / dayMs);
  if (diffDays <= 0) return "Today";
  if (diffDays === 1) return "Yesterday";
  if (diffDays < 7) return "This week";
  return "Older";
}

const BUCKET_ORDER = ["Today", "Yesterday", "This week", "Older"];

export function SessionSidebar({
  activeId,
  onSelect,
  onNew,
  refreshKey,
  collapsed,
  mobileOpen,
  onClose,
}: {
  activeId: string | null;
  onSelect: (storedId: string) => void;
  onNew: () => void;
  refreshKey: number;
  /** Desktop: hidden when true. */
  collapsed: boolean;
  /** Mobile: drawer is open when true. */
  mobileOpen: boolean;
  /** Collapse (desktop) / close the drawer (mobile). */
  onClose: () => void;
}) {
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await api.getSessions(40, 0, getManagementProfile(), "recent");
      setSessions(r.sessions);
    } catch {
      /* connection/auth issues surface elsewhere */
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  const rename = async (s: SessionInfo) => {
    const title = window.prompt("Rename chat", s.title ?? s.preview ?? "");
    if (title == null) return;
    try {
      await api.renameSession(s.id, title.trim());
      await load();
    } catch {
      /* ignore */
    }
  };

  const remove = async (s: SessionInfo) => {
    if (!window.confirm("Delete this chat? This cannot be undone.")) return;
    try {
      await api.deleteSession(s.id);
      if (s.id === activeId) onNew();
      await load();
    } catch {
      /* ignore */
    }
  };

  const grouped = useMemo(() => {
    const q = query.trim().toLowerCase();
    const filtered = q
      ? sessions.filter((s) =>
          `${s.title ?? ""} ${s.preview ?? ""}`.toLowerCase().includes(q),
        )
      : sessions;
    const buckets = new Map<string, SessionInfo[]>();
    for (const s of filtered) {
      const b = bucketOf(s.last_active);
      const arr = buckets.get(b);
      if (arr) arr.push(s);
      else buckets.set(b, [s]);
    }
    return BUCKET_ORDER.filter((b) => buckets.has(b)).map((b) => ({
      label: b,
      sessions: buckets.get(b)!,
    }));
  }, [sessions, query]);

  const empty = !loading && sessions.length === 0;
  const noMatches = !loading && sessions.length > 0 && grouped.length === 0;

  return (
    <div
      className={cn(
        "flex w-64 shrink-0 flex-col border-r border-border bg-background",
        // Mobile: fixed slide-in drawer over the chat.
        "max-lg:fixed max-lg:inset-y-0 max-lg:left-0 max-lg:z-50 max-lg:shadow-xl",
        "max-lg:transition-transform max-lg:duration-200 max-lg:ease-out",
        mobileOpen ? "max-lg:translate-x-0" : "max-lg:-translate-x-full",
        // Desktop: hide entirely when collapsed (chat takes the full width).
        collapsed && "lg:hidden",
      )}
    >
      <div className="flex items-center gap-1 p-2">
        <Button outlined size="sm" className="flex-1 justify-start" onClick={onNew}>
          <MessageSquarePlus className="size-4" />
          New chat
        </Button>
        <Button
          ghost
          size="icon"
          onClick={onClose}
          aria-label="Hide sessions"
          title="Hide sessions"
        >
          <X className="size-4 lg:hidden" />
          <PanelLeftClose className="hidden size-4 lg:block" />
        </Button>
      </div>
      <div className="px-2 pb-2">
        <div className="flex items-center gap-1.5 rounded-md border border-border/60 bg-muted/20 px-2 py-1 focus-within:border-primary/40">
          <Search className="size-3.5 shrink-0 text-muted-foreground" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search chats…"
            className="w-full bg-transparent text-xs outline-none placeholder:text-muted-foreground/60"
          />
          {query && (
            <button
              type="button"
              aria-label="Clear search"
              onClick={() => setQuery("")}
              className="rounded p-0.5 text-muted-foreground hover:text-foreground"
            >
              <X className="size-3" />
            </button>
          )}
        </div>
      </div>
      <div className="chat-scroll flex-1 overflow-auto px-1 pb-2">
        {loading && sessions.length === 0 && (
          <div className="flex items-center gap-2 p-3 text-xs text-muted-foreground">
            <Loader2 className="size-3 animate-spin" />
            Loading…
          </div>
        )}
        {empty && <div className="p-3 text-xs text-muted-foreground">No chats yet.</div>}
        {noMatches && (
          <div className="p-3 text-xs text-muted-foreground">No chats match “{query}”.</div>
        )}
        {grouped.map((g) => (
          <div key={g.label}>
            <div className="px-2 pb-1 pt-3 text-[10px] font-medium uppercase tracking-wider text-muted-foreground/60 first:pt-1">
              {g.label}
            </div>
            {g.sessions.map((s) => (
              <div
                key={s.id}
                onClick={() => onSelect(s.id)}
                className={cn(
                  "group relative flex cursor-pointer items-center gap-1 rounded-md px-2 py-1.5 text-sm hover:bg-muted/50",
                  s.id === activeId && "bg-muted",
                )}
              >
                {s.id === activeId && (
                  <span className="absolute inset-y-1.5 left-0 w-0.5 rounded-full bg-primary" />
                )}
                <div className="min-w-0 flex-1">
                  <div className="truncate">{s.title || s.preview || "Untitled"}</div>
                  <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
                    {s.source && s.source !== "web" && (
                      <span className="rounded bg-muted px-1 uppercase">{s.source}</span>
                    )}
                    <span>{ago(s.last_active)}</span>
                    <span>· {s.message_count} msgs</span>
                  </div>
                </div>
                <div className="hidden shrink-0 gap-0.5 group-hover:flex">
                  <button
                    type="button"
                    title="Rename"
                    onClick={(e) => {
                      e.stopPropagation();
                      void rename(s);
                    }}
                    className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
                  >
                    <Pencil className="size-3.5" />
                  </button>
                  <button
                    type="button"
                    title="Delete"
                    onClick={(e) => {
                      e.stopPropagation();
                      void remove(s);
                    }}
                    className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-destructive"
                  >
                    <Trash2 className="size-3.5" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
