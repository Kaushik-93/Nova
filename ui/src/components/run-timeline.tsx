"use client";

import { Check, ChevronRight, Loader2, X } from "lucide-react";

import { useNow } from "@/lib/use-run";
import { AGENT_NAMES, type Stage, type Step } from "@/lib/steps";
import { cn, formatSeconds } from "@/lib/utils";

function StepRow({ step, now }: { step: Step; now: number }) {
  const elapsed = step.running ? (now - new Date(step.startedAt).getTime()) / 1000 : step.seconds;
  return (
    <li className="py-1">
      <div className="flex items-start gap-2 text-xs">
        {step.running ? <Loader2 className="mt-0.5 size-3 shrink-0 animate-spin text-primary" />
          : step.failed ? <X className="mt-0.5 size-3 shrink-0 text-destructive" />
          : <Check className="mt-0.5 size-3 shrink-0 text-muted-foreground" />}
        <div className="min-w-0 flex-1">
          <div className={cn("flex items-baseline justify-between gap-2", step.running && "font-medium")}>
            <span>{step.label}</span>
            {elapsed != null && elapsed >= 0.5 && (
              <span className={cn("shrink-0 tabular-nums text-muted-foreground", elapsed > 15 && "text-warning")}>{formatSeconds(elapsed)}</span>
            )}
          </div>
          {step.detail && <p className="mt-0.5 truncate font-mono text-[11px] text-muted-foreground" title={step.detail}>{step.detail}</p>}
          {step.text && (
            <details className="mt-1">
              <summary className="cursor-pointer text-[11px] font-medium text-primary">What it said</summary>
              <p className="mt-1 whitespace-pre-wrap rounded-lg bg-muted p-2.5 text-[11px] leading-relaxed">{step.text}</p>
            </details>
          )}
        </div>
      </div>
    </li>
  );
}

export function RunTimeline({ stages, active }: { stages: Stage[]; active: boolean }) {
  const now = useNow(active);
  return (
    <ol className="space-y-1.5">
      {stages.map((stage, index) => {
        const steps = stage.steps.filter((step) => !step.quiet);
        return (
          <li key={stage.key}>
            <details open={stage.running || index === stages.length - 1} className="group rounded-lg border border-border">
              <summary className="flex cursor-pointer list-none items-center gap-2 px-2.5 py-2 [&::-webkit-details-marker]:hidden">
                <ChevronRight className="size-3 text-muted-foreground transition-transform group-open:rotate-90" />
                <span className="text-xs font-medium">{AGENT_NAMES[stage.agent] ?? stage.agent}</span>
                {stage.running && <Loader2 className="size-3 animate-spin text-primary" />}
                <span className="ml-auto text-[11px] tabular-nums text-muted-foreground">{formatSeconds(stage.seconds)}</span>
              </summary>
              <ul className="border-t border-border px-2.5 py-1">
                {steps.map((step) => <StepRow key={step.id} step={step} now={now} />)}
              </ul>
            </details>
          </li>
        );
      })}
    </ol>
  );
}
