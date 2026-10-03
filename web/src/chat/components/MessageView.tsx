/**
 * MessageView — renders one ChatMessage by walking its ordered parts, so
 * reasoning / tool calls / text appear in the exact order they streamed in.
 *
 * Long agentic turns are kept calm by folding runs of ≥3 consecutive
 * tool/subagent parts into a collapsible ActivityGroup: expanded while any
 * step is still running, collapsed to a one-line summary once the run is done
 * (a manual toggle always wins).
 */

import { memo, useState } from "react";
import { Check, ChevronRight, Copy, Layers, Sparkles } from "lucide-react";
import { Markdown, copyText } from "@/components/Markdown";
import { useAgentName } from "@/lib/brand";
import { cn } from "@/lib/utils";
import type { ChatMessage, Part, SubagentPart, ToolPart } from "../model";
import { ReasoningPane } from "./ReasoningPane";
import { ToolCard } from "./ToolCard";
import { SubagentCard } from "./SubagentCard";

type StepPart = ToolPart | SubagentPart;
type RenderItem =
  | { kind: "part"; part: Part; index: number }
  | { kind: "group"; parts: StepPart[]; index: number };

const MIN_GROUP_RUN = 3;

const isStep = (p: Part): p is StepPart => p.type === "tool" || p.type === "subagent";

/** Fold runs of ≥MIN_GROUP_RUN consecutive tool/subagent parts into groups. */
function toRenderItems(parts: Part[]): RenderItem[] {
  const items: RenderItem[] = [];
  let i = 0;
  while (i < parts.length) {
    const part = parts[i];
    if (isStep(part)) {
      const run: StepPart[] = [];
      let j = i;
      while (j < parts.length && isStep(parts[j])) {
        run.push(parts[j] as StepPart);
        j++;
      }
      if (run.length >= MIN_GROUP_RUN) {
        items.push({ kind: "group", parts: run, index: i });
      } else {
        run.forEach((p, k) => items.push({ kind: "part", part: p, index: i + k }));
      }
      i = j;
      continue;
    }
    items.push({ kind: "part", part, index: i });
    i++;
  }
  return items;
}

function StepView({ part }: { part: StepPart }) {
  return part.type === "tool" ? <ToolCard tool={part} /> : <SubagentCard sub={part} />;
}

function ActivityGroup({ parts }: { parts: StepPart[] }) {
  // null → derived default (open while running, collapsed when done).
  const [override, setOverride] = useState<boolean | null>(null);
  const running = parts.some((p) => p.status === "running");
  const open = override ?? running;
  const failed = parts.filter(
    (p) => p.status === "error" || p.status === "interrupted",
  ).length;
  const totalS = parts.reduce(
    (acc, p) => acc + (typeof p.durationS === "number" ? p.durationS : 0),
    0,
  );

  return (
    <div className="rounded-lg border border-border/60 bg-muted/10">
      <button
        type="button"
        onClick={() => setOverride(!open)}
        className="flex w-full cursor-pointer items-center gap-1.5 px-2.5 py-1.5 text-left text-xs text-muted-foreground hover:bg-muted/30"
      >
        <Layers className="size-3.5 shrink-0" />
        <span className="font-medium text-foreground/90">
          {running ? `Working — step ${parts.length}` : `Ran ${parts.length} steps`}
        </span>
        {failed > 0 && (
          <span className="rounded bg-red-500/15 px-1 py-px text-[10px] font-medium text-red-400">
            {failed} failed
          </span>
        )}
        <span className="ml-auto flex shrink-0 items-center gap-1.5">
          {totalS > 0 && <span>{totalS.toFixed(1)}s</span>}
          <ChevronRight className={cn("size-3.5 transition-transform", open && "rotate-90")} />
        </span>
      </button>
      {open && (
        <div className="space-y-1.5 border-t border-border/50 p-1.5">
          {parts.map((p, i) => (
            <StepView key={p.id || i} part={p} />
          ))}
        </div>
      )}
    </div>
  );
}

function CopyMessage({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      title="Copy message"
      aria-label="Copy message"
      onClick={() => {
        void copyText(text).then((ok) => {
          if (!ok) return;
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1500);
        });
      }}
      className="ml-auto rounded p-1 text-muted-foreground opacity-0 transition-all hover:bg-muted hover:text-foreground group-hover/msg:opacity-100"
    >
      {copied ? <Check className="size-3.5 text-success" /> : <Copy className="size-3.5" />}
    </button>
  );
}

function timeLabel(ts: number): string {
  if (!ts) return "";
  try {
    return new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch {
    return "";
  }
}

// memo: the reducer preserves reference identity for every message except the
// streaming one (withAssistant clones only the tail), so per-delta re-renders
// collapse from the whole transcript to a single message subtree.
export const MessageView = memo(function MessageView({ msg }: { msg: ChatMessage }) {
  const agentName = useAgentName();
  if (msg.role === "user") {
    return (
      <div className="chat-msg-in flex justify-end">
        <div className="max-w-[80%] whitespace-pre-wrap break-words rounded-2xl rounded-br-md bg-primary px-3.5 py-2 text-sm text-primary-foreground shadow-sm">
          {msg.parts.map((p) => (p.type === "text" ? p.text : "")).join("")}
        </div>
      </div>
    );
  }

  if (msg.role === "system") {
    return (
      <div className="chat-msg-in text-center">
        <span className="inline-block max-w-full break-words rounded-full border border-border/60 bg-muted/30 px-3 py-1 text-xs text-muted-foreground">
          {msg.parts.map((p) => (p.type === "text" ? p.text : "")).join("")}
        </span>
      </div>
    );
  }

  // Assistant: render parts in arrival order. The caret hugs the final text part
  // while the turn is still streaming.
  const lastTextIdx = msg.parts.reduce(
    (acc, p, i) => (p.type === "text" ? i : acc),
    -1,
  );
  const plainText = msg.parts
    .filter((p): p is Extract<Part, { type: "text" }> => p.type === "text")
    .map((p) => p.text)
    .join("\n\n")
    .trim();
  const time = timeLabel(msg.createdAt);

  return (
    <div className="group/msg chat-msg-in flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <span className="flex size-5 items-center justify-center rounded-md border border-border/60 bg-card">
          <Sparkles className="size-3 text-primary" />
        </span>
        <span className="text-xs font-medium text-muted-foreground">{agentName}</span>
        {time && <span className="text-[10px] text-muted-foreground/50">{time}</span>}
        {!msg.pending && plainText && <CopyMessage text={plainText} />}
      </div>
      {toRenderItems(msg.parts).map((item) => {
        if (item.kind === "group") {
          return (
            <ActivityGroup
              key={item.parts[0].id || `g${item.index}`}
              parts={item.parts}
            />
          );
        }
        const part = item.part;
        const i = item.index;
        switch (part.type) {
          case "reasoning":
            return <ReasoningPane key={i} text={part.text} />;
          case "tool":
            return <ToolCard key={part.id || i} tool={part} />;
          case "subagent":
            return <SubagentCard key={part.id || i} sub={part} />;
          case "text":
            return (
              <div key={i} className="text-sm">
                <Markdown content={part.text} streaming={msg.pending && i === lastTextIdx} />
              </div>
            );
        }
      })}
      {/* Empty pending turn (no parts yet) shows a subtle placeholder bar. */}
      {msg.pending && msg.parts.length === 0 && (
        <div className={cn("h-4 w-24 animate-pulse rounded bg-muted")} />
      )}
    </div>
  );
});
