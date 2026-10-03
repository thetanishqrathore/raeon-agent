/**
 * ToolCard — renders one tool call (running → done/error) with its "which site /
 * which skill" context, duration, expandable result/args, and inline diff.
 */

import {
  Check,
  ChevronRight,
  Copy,
  FileText,
  Globe,
  Loader2,
  Search,
  Sparkles,
  TerminalSquare,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/utils";
import { copyText } from "@/components/Markdown";
import type { ToolPart } from "../model";

// Icon + accent per tool family. Accents are tuned for the DS's dark canvases
// (every theme preset ships a dark background, and no `.dark` class is ever
// applied, so `dark:` variants would be dead code here).
const TOOL_STYLES: Record<string, { icon: LucideIcon; accent: string }> = {
  web_search: { icon: Search, accent: "text-sky-400" },
  web_extract: { icon: Globe, accent: "text-violet-400" },
  browser_navigate: { icon: Globe, accent: "text-violet-400" },
  read_file: { icon: FileText, accent: "text-amber-300" },
  write_file: { icon: FileText, accent: "text-amber-300" },
  edit_file: { icon: FileText, accent: "text-amber-300" },
  terminal: { icon: TerminalSquare, accent: "text-emerald-400" },
  process: { icon: TerminalSquare, accent: "text-emerald-400" },
  skill_view: { icon: Sparkles, accent: "text-fuchsia-400" },
  skill_manage: { icon: Sparkles, accent: "text-fuchsia-400" },
  delegate_task: { icon: Sparkles, accent: "text-fuchsia-400" },
};

function styleFor(name: string): { icon: LucideIcon; accent: string } {
  return TOOL_STYLES[name] ?? { icon: Wrench, accent: "text-primary/80" };
}

function DiffView({ diff }: { diff: string }) {
  return (
    <pre className="mt-1 max-h-64 overflow-auto rounded bg-background/60 p-2 font-mono text-[11px] leading-snug">
      {diff.split("\n").map((line, i) => {
        const add = line.startsWith("+") && !line.startsWith("+++");
        const del = line.startsWith("-") && !line.startsWith("---");
        return (
          <div
            key={i}
            className={cn(
              "whitespace-pre-wrap break-words",
              add && "bg-green-500/10 text-green-400",
              del && "bg-red-500/10 text-red-400",
            )}
          >
            {line || " "}
          </div>
        );
      })}
    </pre>
  );
}

function CopyResult({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      title="Copy output"
      aria-label="Copy output"
      onClick={(e) => {
        e.stopPropagation();
        void copyText(text).then((ok) => {
          if (!ok) return;
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1500);
        });
      }}
      className="rounded p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
    >
      {copied ? <Check className="size-3 text-success" /> : <Copy className="size-3" />}
    </button>
  );
}

export function ToolCard({ tool }: { tool: ToolPart }) {
  const [open, setOpen] = useState(false);
  const { icon: Icon, accent } = styleFor(tool.name);
  const hasDetail = Boolean(tool.result || tool.inlineDiff || tool.argsText);

  return (
    <div
      className={cn(
        "rounded-lg border bg-muted/30 text-xs transition-colors",
        tool.status === "error" ? "border-red-500/40" : "border-border/60",
        hasDetail && "hover:border-border",
      )}
    >
      <button
        type="button"
        onClick={() => hasDetail && setOpen((v) => !v)}
        className={cn(
          "flex w-full items-center gap-1.5 px-2.5 py-1.5 text-left",
          hasDetail && "cursor-pointer",
        )}
      >
        {tool.status === "running" ? (
          <Loader2 className="size-3.5 shrink-0 animate-spin text-amber-400" />
        ) : (
          <Icon
            className={cn(
              "size-3.5 shrink-0",
              tool.status === "error" ? "text-red-400" : accent,
            )}
          />
        )}
        <span className="font-mono font-medium">{tool.name}</span>
        {tool.context && (
          <span className="min-w-0 truncate text-muted-foreground">· {tool.context}</span>
        )}
        <span className="ml-auto flex shrink-0 items-center gap-1.5 text-muted-foreground">
          {tool.status === "error" && (
            <span className="rounded bg-red-500/15 px-1 py-px text-[10px] font-medium text-red-400">
              failed
            </span>
          )}
          {typeof tool.durationS === "number" && <span>{tool.durationS.toFixed(1)}s</span>}
          {hasDetail && (
            <ChevronRight className={cn("size-3.5 transition-transform", open && "rotate-90")} />
          )}
        </span>
      </button>

      {open && hasDetail && (
        <div className="border-t border-border/50 px-2.5 pb-2 pt-1.5">
          {tool.argsText && !tool.inlineDiff && (
            <div className="mb-1.5">
              <div className="mb-0.5 text-[10px] uppercase tracking-wider text-muted-foreground/70">
                args
              </div>
              <pre className="max-h-32 overflow-auto whitespace-pre-wrap break-words rounded bg-background/40 p-1.5 font-mono text-[11px] leading-snug text-muted-foreground">
                {tool.argsText}
              </pre>
            </div>
          )}
          {tool.inlineDiff ? (
            <DiffView diff={tool.inlineDiff} />
          ) : tool.result ? (
            <div>
              <div className="mb-0.5 flex items-center justify-between">
                <div className="text-[10px] uppercase tracking-wider text-muted-foreground/70">
                  output
                </div>
                <CopyResult text={tool.result} />
              </div>
              <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] leading-snug text-muted-foreground">
                {tool.result}
              </pre>
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}
