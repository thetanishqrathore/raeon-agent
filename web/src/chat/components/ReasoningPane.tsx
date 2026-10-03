/**
 * ReasoningPane — collapsible "thinking" disclosure for an assistant turn's
 * reasoning tokens. Collapsed by default to keep the transcript calm.
 */

import { Brain, ChevronRight } from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/utils";
import { Markdown } from "@/components/Markdown";

export function ReasoningPane({ text }: { text: string }) {
  const [open, setOpen] = useState(false);

  return (
    <div className="rounded-lg border border-border/50 bg-muted/20 text-xs">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full cursor-pointer items-center gap-1.5 px-2.5 py-1.5 text-left text-muted-foreground hover:bg-muted/40"
      >
        <Brain className="size-3.5 shrink-0" />
        <span>Reasoning</span>
        <ChevronRight className={cn("ml-auto size-3.5 transition-transform", open && "rotate-90")} />
      </button>
      {open && (
        <div className="border-t border-border/50 px-2.5 py-2 opacity-80">
          <Markdown content={text} />
        </div>
      )}
    </div>
  );
}
