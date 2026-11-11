"use client";

import { CalendarRange, Check, ChevronDown, Filter, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ChartCard } from "@/components/charts/chart";
import { api } from "@/lib/api";
import { type ChartData, type DashboardSpec, defaultRows, widths } from "@/lib/charts";
import { cn } from "@/lib/utils";

export type FilterSelection = { name: string; values: string[] }[];

// --- The time filter: presets, resolved to dates today -----------------------------------------------

type Preset = { id: string; label: string; range: () => [string, string] | null };
const iso = (d: Date) => d.toISOString().slice(0, 10);
const utcToday = () => { const now = new Date(); return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate())); };
const monthStart = (d: Date, back = 0) => new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() - back, 1));
const dayBefore = (d: Date) => new Date(d.getTime() - 86_400_000);

const PRESETS: Preset[] = [
  { id: "built", label: "As built", range: () => null },
  { id: "30d", label: "Last 30 days", range: () => { const t = utcToday(); return [iso(new Date(t.getTime() - 29 * 86_400_000)), iso(t)]; } },
  { id: "3m", label: "Last 3 complete months", range: () => { const t = utcToday(); return [iso(monthStart(t, 3)), iso(dayBefore(monthStart(t)))]; } },
  { id: "12m", label: "Last 12 complete months", range: () => { const t = utcToday(); return [iso(monthStart(t, 12)), iso(dayBefore(monthStart(t)))]; } },
  { id: "ytd", label: "This year so far", range: () => { const t = utcToday(); return [`${t.getUTCFullYear()}-01-01`, iso(t)]; } },
  { id: "ly", label: "Last year", range: () => { const y = utcToday().getUTCFullYear() - 1; return [`${y}-01-01`, `${y}-12-31`]; } },
];

function useClickAway(open: boolean, close: () => void) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const handle = (event: MouseEvent | KeyboardEvent) => {
      if (event instanceof KeyboardEvent ? event.key === "Escape" : !ref.current?.contains(event.target as Node)) close();
    };
    document.addEventListener("mousedown", handle);
    document.addEventListener("keydown", handle);
    return () => { document.removeEventListener("mousedown", handle); document.removeEventListener("keydown", handle); };
  }, [open, close]);
  return ref;
}

function FilterControl({ slug, name, chosen, onChange }: { slug: string; name: string; chosen: string[]; onChange: (values: string[]) => void }) {
  const [open, setOpen] = useState(false);
  const [options, setOptions] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const close = useCallback(() => setOpen(false), []);
  const ref = useClickAway(open, close);

  useEffect(() => {
    if (!open || options) return;
    api.filterOptions(slug, name).then(setOptions).catch((cause) => setError(cause.message));
  }, [open, options, slug, name]);

  const shown = (options ?? []).filter((option) => option.toLowerCase().includes(search.toLowerCase())).slice(0, 200);
  const toggle = (value: string) => onChange(chosen.includes(value) ? chosen.filter((v) => v !== value) : [...chosen, value]);
  const summary = chosen.length === 0 ? "All" : chosen.length === 1 ? chosen[0] : `${chosen.length} chosen`;

  return (
    <div ref={ref} className="relative">
      <div className={cn("flex h-8 items-center overflow-hidden rounded-lg border bg-card text-xs", chosen.length ? "border-primary/50" : "border-border")}>
        <button type="button" aria-expanded={open} aria-haspopup="listbox" onClick={() => setOpen(!open)}
          className="flex h-full items-center gap-1.5 px-2.5 hover:bg-muted">
          <Filter className="size-3.5 text-muted-foreground" aria-hidden />
          <span className="text-muted-foreground">{name}</span>
          <span className="max-w-40 truncate font-medium">{summary}</span>
          <ChevronDown className="size-3.5 text-muted-foreground" aria-hidden />
        </button>
        {chosen.length > 0 && (
          <button type="button" aria-label={`Clear ${name}`} onClick={() => onChange([])} className="flex h-full items-center border-l border-border px-1.5 text-muted-foreground hover:bg-muted hover:text-foreground">
            <X className="size-3.5" />
          </button>
        )}
      </div>
      {open && (
        <div className="absolute left-0 top-full z-30 mt-1 w-64 rounded-xl border border-border bg-card p-1.5 shadow-raised">
          <input autoFocus value={search} onChange={(e) => setSearch(e.target.value)} placeholder={`Search ${name.toLowerCase()}…`} aria-label={`Search ${name}`}
            className="mb-1 h-8 w-full rounded-md border border-border bg-background px-2 text-xs outline-none focus:border-primary" />
          <ul role="listbox" aria-multiselectable className="max-h-64 overflow-auto">
            {error && <li className="p-2 text-xs text-destructive">{error}</li>}
            {!options && !error && <li className="p-2 text-xs text-muted-foreground">Loading…</li>}
            {options && shown.length === 0 && <li className="p-2 text-xs text-muted-foreground">Nothing matches.</li>}
            {shown.map((option) => (
              <li key={option} role="option" aria-selected={chosen.includes(option)}>
                <button type="button" onClick={() => toggle(option)} className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs hover:bg-muted">
                  <span className={cn("flex size-4 items-center justify-center rounded border", chosen.includes(option) ? "border-primary bg-primary text-primary-foreground" : "border-border")}>
                    {chosen.includes(option) && <Check className="size-3" />}
                  </span>
                  <span className="truncate">{option}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function TimeControl({ value, onChange }: { value: string; onChange: (id: string) => void }) {
  return (
    <label className={cn("flex h-8 items-center gap-1.5 rounded-lg border bg-card px-2.5 text-xs", value !== "built" ? "border-primary/50" : "border-border")}>
      <CalendarRange className="size-3.5 text-muted-foreground" aria-hidden />
      <span className="text-muted-foreground">Time</span>
      <select value={value} onChange={(e) => onChange(e.target.value)} aria-label="Time window" className="bg-transparent font-medium outline-none">
        {PRESETS.map((preset) => <option key={preset.id} value={preset.id}>{preset.label}</option>)}
      </select>
    </label>
  );
}

// --- Charts, fetched as the person looking -----------------------------------------------------------

function LiveChart({ slug, chart, filters, range, version, className }: { slug: string; chart: DashboardSpec["charts"][number];
  filters: Record<string, string[]>; range: [string, string] | null; version: number; className?: string }) {
  const [data, setData] = useState<ChartData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const request = JSON.stringify({ filters, range, version });

  useEffect(() => {
    let current = true;
    setError(null);
    api.chartData(slug, chart.key, { filters, date_range: range })
      .then((result) => { if (current) setData(result); })
      .catch((cause) => { if (current) setError(cause.message); });
    return () => { current = false; };
  }, [slug, chart.key, request]); // eslint-disable-line react-hooks/exhaustive-deps

  return <ChartCard spec={chart} data={data} error={error} className={className} />;
}

/** A dashboard as Nova draws it. `version` changes when the dashboard was rebuilt, so charts refetch. */
export function Dashboard({ slug, spec, version = 0, onSelection, className }: { slug: string; spec: DashboardSpec; version?: number;
  onSelection?: (selection: FilterSelection) => void; className?: string }) {
  const [chosen, setChosen] = useState<Record<string, string[]>>({});
  const [preset, setPreset] = useState("built");
  const range = useMemo(() => PRESETS.find((p) => p.id === preset)?.range() ?? null, [preset]);
  const rows = spec.rows.length ? spec.rows : defaultRows(spec.charts);
  const byKey = new Map(spec.charts.map((chart) => [chart.key, chart]));

  useEffect(() => {
    const selection = Object.entries(chosen).filter(([, values]) => values.length).map(([name, values]) => ({ name, values }));
    if (preset !== "built") selection.push({ name: "Time", values: [PRESETS.find((p) => p.id === preset)!.label] });
    onSelection?.(selection);
  }, [chosen, preset, onSelection]);

  return (
    <div className={cn("space-y-3 p-4", className)}>
      {(spec.filters.length > 0 || spec.time_filter) && (
        <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Filters">
          {spec.time_filter && <TimeControl value={preset} onChange={setPreset} />}
          {spec.filters.map((filter) => (
            <FilterControl key={filter.name} slug={slug} name={filter.name} chosen={chosen[filter.name] ?? []}
              onChange={(values) => setChosen((current) => ({ ...current, [filter.name]: values }))} />
          ))}
        </div>
      )}
      {rows.map((row, index) => {
        const columns = widths(row);
        return (
          <div key={index} className="grid grid-cols-1 gap-3 md:grid-cols-12">
            {row.map((slot, i) => {
              const chart = byKey.get(slot.key);
              if (!chart) return null;
              return (
                <LiveChart key={slot.key} slug={slug} chart={chart} filters={chosen} range={range} version={version}
                  className={spanClass(columns[i])} />
              );
            })}
          </div>
        );
      })}
    </div>
  );
}

// Tailwind needs whole class names in the source to keep them.
const SPANS = ["", "md:col-span-1", "md:col-span-2", "md:col-span-3", "md:col-span-4", "md:col-span-5", "md:col-span-6",
  "md:col-span-7", "md:col-span-8", "md:col-span-9", "md:col-span-10", "md:col-span-11", "md:col-span-12"];
const spanClass = (columns: number) => SPANS[Math.max(1, Math.min(12, columns))];
