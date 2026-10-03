/**
 * SubagentCard — a delegated subagent's spawn-tree: goal, model, status, and the
 * nested tool steps it ran. Collapsible; expanded shows the step list + summary.
 */

import { useState } from "react";
import { ChevronRight, GitBranch, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import type { SubagentPart } from "../model";

export function SubagentCard({ sub }: { sub: SubagentPart }) {
  const [open, setOpen] = useState(false);
  const hasDetail = sub.steps.length > 0 || Boolean(sub.summary);

  return (
    <div
      className={cn(
        "rounded-lg border bg-muted/20 text-xs",
        sub.status === "interrupted" || sub.status === "error"
          ? "border-red-500/40"
          : "border-border/60",
      )}
    >
      <button
        type="button"
        onClick={() => hasDetail && setOpen((v) => !v)}
        className={cn(
          "flex w-full items-center gap-1.5 px-2.5 py-1.5 text-left",
          hasDetail && "cursor-pointer hover:bg-muted/40",
        )}
      >
        {sub.status === "running" ? (
          <Loader2 className="size-3.5 shrink-0 animate-spin text-amber-500" />
        ) : (
          <GitBranch
            className={cn(
              "size-3.5 shrink-0",
              sub.status === "done" ? "text-green-600 dark:text-green-400" : "text-red-500",
            )}
          />
        )}
        <span className="font-medium">Subagent</span>
        {sub.goal && <span className="min-w-0 truncate text-muted-foreground">· {sub.goal}</span>}
        <span className="ml-auto flex shrink-0 items-center gap-1.5 text-muted-foreground">
          {sub.model && <span className="font-mono">{sub.model}</span>}
          {sub.steps.length > 0 && <span>{sub.steps.length} step{sub.steps.length === 1 ? "" : "s"}</span>}
          {typeof sub.durationS === "number" && <span>{sub.durationS.toFixed(1)}s</span>}
          {hasDetail && (
            <ChevronRight className={cn("size-3.5 transition-transform", open && "rotate-90")} />
          )}
        </span>
      </button>

      {open && hasDetail && (
        <div className="space-y-1 border-t border-border/50 px-2.5 pb-2 pt-1.5">
          {sub.steps.map((s, i) => (
            <div key={i} className="flex items-center gap-1.5 text-muted-foreground">
              <span className="font-mono">{s.tool}</span>
              {s.preview && <span className="min-w-0 truncate">· {s.preview}</span>}
            </div>
          ))}
          {sub.summary && (
            <div className="whitespace-pre-wrap break-words pt-1 text-muted-foreground">
              {sub.summary}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
