"use client";

import { Skeleton } from "@/components/charts/chart";
import type { Plan, RunEvent } from "@/lib/api";
import { KPIS, type Viz } from "@/lib/charts";
import { cn } from "@/lib/utils";

/** The planned charts no chart_ready event has arrived for yet. */
function pending(plan: Plan, events: RunEvent[]) {
  const ready = new Set(events.filter((event) => event.event === "chart_ready").map((event) => event.key));
  return plan.charts.filter((chart) => !ready.has(chart.key));
}

function Placeholder({ title, viz }: { title: string; viz: string }) {
  const kpi = KPIS.has(viz as Viz);
  return (
    <article className={cn("flex flex-col rounded-xl border border-dashed border-border bg-card/60 p-4", kpi ? "h-28" : "h-80")} aria-busy>
      <h3 className="mb-2 truncate text-[13px] font-medium text-muted-foreground" title={title}>{title}</h3>
      <div className="min-h-0 flex-1"><Skeleton kpi={kpi} /></div>
    </article>
  );
}

/** Before any chart exists: one placeholder per planned chart, numbers first. */
export function LiveCanvas({ plan }: { plan: Plan; events: RunEvent[] }) {
  const kpis = plan.charts.filter((chart) => KPIS.has(chart.viz as Viz));
  const charts = plan.charts.filter((chart) => !KPIS.has(chart.viz as Viz));
  return (
    <div className="space-y-3 p-4" data-testid="live-canvas">
      <p className="text-xs text-muted-foreground" role="status" aria-live="polite">{plan.charts.length} charts planned</p>
      {kpis.length > 0 && (
        <div className="grid gap-3" style={{ gridTemplateColumns: `repeat(${Math.min(kpis.length, 4)}, minmax(0, 1fr))` }}>
          {kpis.map((chart) => <Placeholder key={chart.key} title={chart.title} viz={chart.viz} />)}
        </div>
      )}
      <div className="grid gap-3 md:grid-cols-2">
        {charts.map((chart) => <Placeholder key={chart.key} title={chart.title} viz={chart.viz} />)}
      </div>
    </div>
  );
}

/** While a dashboard is being built: the charts still to come, under the ones already drawn. */
export function PendingCharts({ plan, events }: { plan: Plan; events: RunEvent[] }) {
  const left = pending(plan, events);
  if (!left.length) return null;
  return (
    <div className="space-y-3 px-4 pb-4">
      <p className="text-xs text-muted-foreground" role="status" aria-live="polite">
        {plan.charts.length - left.length} of {plan.charts.length} charts ready
      </p>
      <div className="grid gap-3 md:grid-cols-2">
        {left.map((chart) => <Placeholder key={chart.key} title={chart.title} viz={chart.viz} />)}
      </div>
    </div>
  );
}

/** Before there's a plan: the shape of a dashboard, so the page is never blank. */
export function CanvasPlaceholder() {
  return (
    <div className="space-y-3 p-4" aria-hidden>
      <div className="skeleton h-3 w-40 rounded" />
      <div className="grid grid-cols-4 gap-3">
        {[0, 1, 2, 3].map((index) => (
          <div key={index} className="h-28 rounded-xl border border-border bg-card p-4 shadow-card">
            <div className="skeleton h-3 w-24 rounded" /><div className="skeleton mt-4 h-8 w-32 rounded-md" />
          </div>
        ))}
      </div>
      <div className="grid grid-cols-2 gap-3">
        {[0, 1].map((index) => (
          <div key={index} className="h-80 rounded-xl border border-border bg-card p-4 shadow-card">
            <div className="skeleton mb-4 h-3 w-40 rounded" /><div className="h-64"><Skeleton kpi={false} /></div>
          </div>
        ))}
      </div>
    </div>
  );
}
