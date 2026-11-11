"use client";

import type { Timing } from "@/lib/api";
import { AGENT_NAMES, toolLabel } from "@/lib/steps";
import { formatNumber, formatSeconds } from "@/lib/utils";

export function TimingPanel({ timing, wallSeconds }: { timing: Timing; wallSeconds: number }) {
  const agents = Object.entries(timing.agents).sort(([, a], [, b]) => b.model_seconds + b.tool_seconds - a.model_seconds - a.tool_seconds);
  const longest = Math.max(1, ...agents.map(([, a]) => a.model_seconds + a.tool_seconds));
  return (
    <div className="space-y-5">
      <dl className="grid grid-cols-3 divide-x divide-border rounded-lg border border-border">
        {[["Elapsed", formatSeconds(wallSeconds)], ["Model calls", String(timing.turns)], ["Tokens", formatNumber(timing.tokens)]].map(([label, value]) => (
          <div key={label} className="px-3 py-2.5">
            <dt className="text-[11px] text-muted-foreground">{label}</dt>
            <dd className="mt-0.5 text-sm font-semibold">{value}</dd>
          </div>
        ))}
      </dl>

      {agents.length > 0 && (
        <div className="space-y-2.5">
          {agents.map(([name, totals]) => {
            const total = totals.model_seconds + totals.tool_seconds;
            return (
              <div key={name}>
                <div className="mb-1 flex justify-between text-xs">
                  <span>{AGENT_NAMES[name] ?? name}</span>
                  <span className="tabular-nums text-muted-foreground">{formatSeconds(total)}</span>
                </div>
                <div className="flex h-1.5 gap-0.5 rounded-full">
                  <div className="rounded-full bg-primary" style={{ width: `${(totals.model_seconds / longest) * 100}%` }} title="Waiting for the model" />
                  <div className="rounded-full bg-warning" style={{ width: `${(totals.tool_seconds / longest) * 100}%` }} title="Data and the semantic layer" />
                </div>
              </div>
            );
          })}
          <p className="flex gap-3 pt-0.5 text-[11px] text-muted-foreground">
            <span className="flex items-center gap-1.5"><i className="h-0.5 w-3 rounded bg-primary" /> Model</span>
            <span className="flex items-center gap-1.5"><i className="h-0.5 w-3 rounded bg-warning" /> Data and the semantic layer</span>
          </p>
        </div>
      )}

      {timing.explanation.length > 0 && (
        <ul className="space-y-1.5 text-xs leading-relaxed text-muted-foreground">
          {timing.explanation.map((note) => <li key={note}>{note}</li>)}
        </ul>
      )}

      {timing.slowest.length > 0 && (
        <div>
          <h3 className="mb-1.5 text-[11px] font-medium text-muted-foreground">Slowest steps</h3>
          <ul className="space-y-1 text-xs">
            {timing.slowest.map((step, index) => (
              <li key={index} className="flex justify-between gap-2">
                <span className="truncate">{AGENT_NAMES[step.agent] ?? step.agent}: {toolLabel(step.label).toLowerCase()}</span>
                <span className="tabular-nums text-muted-foreground">{formatSeconds(step.seconds)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
