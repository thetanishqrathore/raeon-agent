/**
 * PromptPanel — renders a blocking interactive request (clarify / approval /
 * sudo / secret) and sends the matching *.respond RPC. The agent's turn is
 * paused until one of these resolves, so this panel takes over the composer.
 */

import { useState } from "react";
import { HelpCircle, KeyRound, Loader2, ShieldAlert } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { useAgentName } from "@/lib/brand";
import { cn } from "@/lib/utils";
import type { GatewayClient } from "@/lib/gatewayClient";
import {
  respondApproval,
  respondClarify,
  respondSecret,
  respondSudo,
} from "../gateway/protocol";
import type { PendingPrompt } from "../model";

export function PromptPanel({
  prompt,
  client,
  sessionId,
  onResolved,
}: {
  prompt: PendingPrompt;
  client: GatewayClient;
  sessionId: string | null;
  onResolved: () => void;
}) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const agentName = useAgentName();

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      onResolved();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const Icon =
    prompt.kind === "approval"
      ? ShieldAlert
      : prompt.kind === "clarify"
        ? HelpCircle
        : KeyRound;

  const accent =
    prompt.kind === "approval" ? "border-amber-500/50" : "border-primary/40";

  return (
    <div className={cn("mx-auto w-full max-w-3xl rounded-lg border bg-muted/30 p-3", accent)}>
      <div className="mb-2 flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
        <Icon className="size-3.5" />
        {prompt.kind === "approval"
          ? "Approval required"
          : prompt.kind === "clarify"
            ? `${agentName} needs clarification`
            : prompt.kind === "sudo"
              ? "Password required"
              : "Secret required"}
      </div>

      {prompt.kind === "clarify" && (
        <div className="space-y-2">
          {prompt.question && <div className="text-sm">{prompt.question}</div>}
          {prompt.choices ? (
            <div className="flex flex-wrap gap-2">
              {prompt.choices.map((c) => (
                <Button
                  key={c}
                  outlined
                  size="sm"
                  disabled={busy}
                  onClick={() => void run(() => respondClarify(client, prompt.requestId, c))}
                >
                  {c}
                </Button>
              ))}
            </div>
          ) : (
            <ClarifyInput
              value={value}
              setValue={setValue}
              busy={busy}
              onSubmit={() => value.trim() && void run(() => respondClarify(client, prompt.requestId, value.trim()))}
            />
          )}
        </div>
      )}

      {prompt.kind === "approval" && (
        <div className="space-y-2">
          {prompt.command && (
            <pre className="overflow-auto rounded bg-background/60 p-2 font-mono text-xs">
              {prompt.command}
            </pre>
          )}
          {prompt.description && (
            <div className="text-xs text-muted-foreground">{prompt.description}</div>
          )}
          <div className="flex flex-wrap gap-2">
            <Button
              destructive
              size="sm"
              disabled={busy}
              onClick={() => void run(() => respondApproval(client, sessionId ?? "", "deny"))}
            >
              Deny
            </Button>
            <Button
              size="sm"
              disabled={busy}
              onClick={() => void run(() => respondApproval(client, sessionId ?? "", "once"))}
            >
              Allow once
            </Button>
            {prompt.allowPermanent && (
              <Button
                outlined
                size="sm"
                disabled={busy}
                onClick={() => void run(() => respondApproval(client, sessionId ?? "", "always"))}
              >
                Allow always
              </Button>
            )}
          </div>
        </div>
      )}

      {(prompt.kind === "sudo" || prompt.kind === "secret") && (
        <div className="space-y-2">
          {prompt.kind === "secret" && prompt.envVar && (
            <div className="font-mono text-xs">{prompt.envVar}</div>
          )}
          {prompt.promptText && (
            <div className="text-xs text-muted-foreground">{prompt.promptText}</div>
          )}
          <div className="flex items-center gap-2">
            <input
              type="password"
              autoFocus
              value={value}
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && value) {
                  e.preventDefault();
                  const fn =
                    prompt.kind === "sudo"
                      ? () => respondSudo(client, prompt.requestId, value)
                      : () => respondSecret(client, prompt.requestId, value);
                  void run(fn);
                }
              }}
              placeholder="••••••••"
              className="flex-1 rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-ring"
            />
            <Button
              size="sm"
              disabled={busy || !value}
              onClick={() => {
                const fn =
                  prompt.kind === "sudo"
                    ? () => respondSudo(client, prompt.requestId, value)
                    : () => respondSecret(client, prompt.requestId, value);
                void run(fn);
              }}
            >
              {busy ? <Loader2 className="size-4 animate-spin" /> : "Submit"}
            </Button>
          </div>
        </div>
      )}

      {error && <div className="mt-2 text-xs text-destructive">{error}</div>}
    </div>
  );
}

function ClarifyInput({
  value,
  setValue,
  busy,
  onSubmit,
}: {
  value: string;
  setValue: (v: string) => void;
  busy: boolean;
  onSubmit: () => void;
}) {
  return (
    <div className="flex items-center gap-2">
      <input
        autoFocus
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            onSubmit();
          }
        }}
        placeholder="Type your answer…"
        className="flex-1 rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-ring"
      />
      <Button size="sm" disabled={busy || !value.trim()} onClick={onSubmit}>
        {busy ? <Loader2 className="size-4 animate-spin" /> : "Send"}
      </Button>
    </div>
  );
}
