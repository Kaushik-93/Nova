import { Check, Loader2 } from "lucide-react";

import type { Milestone } from "@/lib/steps";
import { cn, formatSeconds } from "@/lib/utils";

export function ProgressSteps({ milestones }: { milestones: Milestone[] }) {
  return (
    <ol aria-label="Progress">
      {milestones.map((milestone, index) => {
        const last = index === milestones.length - 1;
        return (
          <li key={milestone.id} className="relative flex gap-3 pb-3.5 last:pb-0" aria-current={milestone.state === "active" ? "step" : undefined}>
            {!last && <span className={cn("absolute left-[9px] top-5 h-[calc(100%-16px)] w-px", milestone.state === "done" ? "bg-primary" : "bg-border")} />}
            <span className={cn("relative z-10 flex size-[19px] shrink-0 items-center justify-center rounded-full",
              milestone.state === "done" && "bg-primary text-primary-foreground",
              milestone.state === "active" && "bg-accent text-primary",
              milestone.state === "pending" && "border border-border bg-card")}>
              {milestone.state === "done" && <Check className="size-3" strokeWidth={3} />}
              {milestone.state === "active" && <Loader2 className="size-3 animate-spin" strokeWidth={3} />}
            </span>
            <div className="flex min-w-0 flex-1 items-baseline justify-between gap-2">
              <div className="min-w-0">
                <p className={cn("text-[13px]", milestone.state === "pending" ? "text-muted-foreground" : "font-medium", milestone.state === "active" && "text-primary")}>{milestone.label}</p>
                {milestone.note && <p className="text-[11px] text-muted-foreground">{milestone.note}</p>}
              </div>
              {milestone.seconds != null && <span className="shrink-0 text-[11px] tabular-nums text-muted-foreground">{formatSeconds(milestone.seconds)}</span>}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
