"use client";

import { Clock } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { Timing } from "@/lib/api";
import { AGENT_NAMES } from "@/lib/steps";
import { toolLabel } from "@/lib/steps";
import { formatNumber, formatSeconds } from "@/lib/utils";

export function TimingPanel({ timing, wallSeconds }: { timing: Timing; wallSeconds: number }) {
  const agents = Object.entries(timing.agents).sort(([, a], [, b]) => b.model_seconds + b.tool_seconds - a.model_seconds - a.tool_seconds);
  const longest = Math.max(1, ...agents.map(([, a]) => a.model_seconds + a.tool_seconds));
  return (
    <Card>
      <CardHeader className="flex items-center gap-2"><Clock className="size-4 text-muted-foreground" /><CardTitle>Where the time went</CardTitle></CardHeader>
      <CardContent className="space-y-4">
        <dl className="grid grid-cols-3 gap-3 text-center">
          <div><dt className="text-[11px] text-muted-foreground">Elapsed</dt><dd className="text-sm font-semibold tabular-nums">{formatSeconds(wallSeconds)}</dd></div>
          <div><dt className="text-[11px] text-muted-foreground">Model calls</dt><dd className="text-sm font-semibold tabular-nums">{timing.turns}</dd></div>
          <div><dt className="text-[11px] text-muted-foreground">Tokens</dt><dd className="text-sm font-semibold tabular-nums">{formatNumber(timing.tokens)}</dd></div>
        </dl>

        {agents.length > 0 && (
          <div className="space-y-2">
            {agents.map(([name, totals]) => {
              const total = totals.model_seconds + totals.tool_seconds;
              return (
                <div key={name}>
                  <div className="mb-1 flex justify-between text-xs">
                    <span>{AGENT_NAMES[name] ?? name}</span>
                    <span className="tabular-nums text-muted-foreground">{formatSeconds(total)}</span>
                  </div>
                  <div className="flex h-1.5 overflow-hidden rounded-full bg-muted">
                    <div className="bg-primary" style={{ width: `${(totals.model_seconds / longest) * 100}%` }} title="Waiting for the model" />
                    <div className="bg-warning" style={{ width: `${(totals.tool_seconds / longest) * 100}%` }} title="Data and Superset" />
                  </div>
                </div>
              );
            })}
            <p className="flex gap-3 pt-1 text-[11px] text-muted-foreground">
              <span className="flex items-center gap-1"><i className="size-2 rounded-full bg-primary" /> Model</span>
              <span className="flex items-center gap-1"><i className="size-2 rounded-full bg-warning" /> Data and Superset</span>
            </p>
          </div>
        )}

        {timing.explanation.length > 0 && (
          <ul className="space-y-1.5 text-xs leading-relaxed">
            {timing.explanation.map((note) => <li key={note}>{note}</li>)}
          </ul>
        )}

        {timing.slowest.length > 0 && (
          <details>
            <summary className="cursor-pointer text-xs text-primary">Slowest steps</summary>
            <ul className="mt-2 space-y-1 text-xs">
              {timing.slowest.map((step, index) => (
                <li key={index} className="flex justify-between gap-2">
                  <span className="truncate">{AGENT_NAMES[step.agent] ?? step.agent}: {toolLabel(step.label).toLowerCase()}</span>
                  <span className="tabular-nums text-muted-foreground">{formatSeconds(step.seconds)}</span>
                </li>
              ))}
            </ul>
          </details>
        )}
      </CardContent>
    </Card>
  );
}
