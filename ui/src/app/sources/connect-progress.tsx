"use client";

import { CheckCircle2, Circle, Loader2, XCircle } from "lucide-react";
import { useEffect } from "react";

import { Card } from "@/components/ui/card";
import { useRun } from "@/lib/use-run";
import { cn } from "@/lib/utils";

const STEPS = [
  { key: "superset", label: "Registering it with Superset" },
  { key: "catalog", label: "Adding it to the query engine" },
  { key: "profile", label: "Learning the data" },
] as const;

export function ConnectProgress({ runId, name, onDone }: { runId: string; name: string; onDone: () => void }) {
  const { events, run } = useRun(runId);
  const finished = Boolean(run && run.status !== "running");
  useEffect(() => { if (finished) onDone(); }, [finished]);

  const steps = events.filter((event) => event.event === "connect_step").map((event) => event.step);
  const current = steps.at(-1);
  const failed = run?.status === "failed";
  const table = events.filter((event) => event.event === "profile_table").at(-1);
  const measured = table && table.done !== undefined && table.done < (table.total ?? 0);
  const detail = measured ? `Measuring ${table.table} (${table.done} of ${table.total})` : table ? "Writing what it learned, and checking it" : null;

  return (
    <Card className="p-4" role="status">
      <p className="text-[13px] font-semibold">{failed ? `${name} couldn't be connected` : run?.status === "finished" ? `${name} is ready` : `Connecting ${name}`}</p>
      {failed && <p className="mt-1 text-xs text-destructive">{run?.result?.error ?? "Something went wrong."}</p>}
      <ol className="mt-3 space-y-1.5">
        {STEPS.map((step, index) => {
          const position = STEPS.findIndex((item) => item.key === current);
          const done = run?.status === "finished" || index < position;
          const active = index === position && !done;
          return (
            <li key={step.key} className={cn("flex items-center gap-2 text-xs", !done && !active && "text-muted-foreground")}>
              {done ? <CheckCircle2 className="size-3.5 text-success" />
                : active && failed ? <XCircle className="size-3.5 text-destructive" />
                : active ? <Loader2 className="size-3.5 animate-spin text-primary" />
                : <Circle className="size-3.5" />}
              <span>{step.label}{active && step.key === "profile" && detail ? `: ${detail}` : ""}</span>
            </li>
          );
        })}
      </ol>
    </Card>
  );
}
