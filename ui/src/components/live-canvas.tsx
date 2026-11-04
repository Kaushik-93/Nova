"use client";

import { useEffect, useMemo, useState } from "react";

import { ChartPreview, isKpi } from "@/components/chart-preview";
import { api, type ChartPreview as Preview, type Plan, type RunEvent } from "@/lib/api";
import { cn } from "@/lib/utils";

type Slot = { id: string; title: string; viz: string; chartId?: number; version?: number };

/** One slot per planned chart, filled in as charts are finished. A chart that wasn't planned,
 *  or whose key changed, still gets shown. */
function slotsFrom(plan: Plan | undefined, events: RunEvent[]): Slot[] {
  const ready = events.filter((event) => event.event === "chart_ready");
  const slots: Slot[] = (plan?.charts ?? []).map((chart) => ({ id: chart.key, title: chart.title, viz: chart.viz }));
  const same = (a: string, b: string) => a.trim().toLowerCase() === b.trim().toLowerCase();
  for (const event of ready) {
    let slot = slots.find((candidate) => candidate.id === event.key) ?? slots.find((candidate) => !candidate.chartId && same(candidate.title, event.title ?? ""));
    if (!slot) {
      slot = { id: event.key ?? String(event.id), title: event.title ?? "", viz: event.viz ?? "table" };
      slots.push(slot);
    }
    slot.chartId = event.chart_id;
    slot.version = event.id;
    slot.title = event.title ?? slot.title;
    slot.viz = event.viz ?? slot.viz;
  }
  return slots;
}

function Skeleton({ kpi }: { kpi: boolean }) {
  if (kpi) return <div className="skeleton mt-1 h-8 w-32 rounded-md" />;
  return (
    <div className="flex h-full flex-col justify-end gap-2">
      <div className="flex h-full items-end gap-2">
        {[38, 62, 48, 74, 56, 84, 68, 92, 76].map((height, index) => (
          <div key={index} className="skeleton flex-1 rounded-t" style={{ height: `${height}%` }} />
        ))}
      </div>
      <div className="skeleton h-2 w-full rounded" />
    </div>
  );
}

function SlotCard({ slot }: { slot: Slot }) {
  const [preview, setPreview] = useState<Preview | null>(null);

  useEffect(() => {
    if (!slot.chartId) return;
    let current = true;
    api.chartPreview(slot.chartId).then((data) => { if (current) setPreview(data); }).catch(() => undefined);
    return () => { current = false; };
  }, [slot.chartId, slot.version]);

  const kpi = isKpi(slot.viz);
  return (
    <article className={cn("flex flex-col rounded-xl border border-border bg-card p-4 shadow-card", kpi ? "h-28" : "h-80")} aria-busy={!preview}>
      <h3 className="mb-2 truncate text-[13px] font-medium text-muted-foreground" title={slot.title}>{slot.title}</h3>
      <div className="min-h-0 flex-1">
        {preview ? <div className="rise h-full"><ChartPreview preview={preview} /></div> : <Skeleton kpi={kpi} />}
      </div>
    </article>
  );
}

export function LiveCanvas({ plan, events }: { plan?: Plan; events: RunEvent[] }) {
  const slots = useMemo(() => slotsFrom(plan, events), [plan, events]);
  const kpis = slots.filter((slot) => isKpi(slot.viz));
  const charts = slots.filter((slot) => !isKpi(slot.viz));
  const ready = slots.filter((slot) => slot.chartId).length;

  return (
    <div className="space-y-3 p-4" data-testid="live-canvas">
      {slots.length > 0 && (
        <p className="text-xs text-muted-foreground" role="status" aria-live="polite">
          {ready === 0 ? `${slots.length} charts planned` : `${ready} of ${slots.length} charts ready`}
        </p>
      )}
      {kpis.length > 0 && (
        <div className="grid gap-3" style={{ gridTemplateColumns: `repeat(${Math.min(kpis.length, 4)}, minmax(0, 1fr))` }}>
          {kpis.map((slot) => <SlotCard key={slot.id} slot={slot} />)}
        </div>
      )}
      <div className="grid grid-cols-2 gap-3">
        {charts.map((slot) => <SlotCard key={slot.id} slot={slot} />)}
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
