"use client";

import { ArrowDownRight, ArrowUpRight, BarChart3, Minus, Table2 } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { countryKey, optionFor, readPalette, treeHeight } from "@/components/charts/options";
import { Certified } from "@/components/semantic";
import { Segmented } from "@/components/ui/segmented";
import { type ChartData, type ChartSpec, KPIS, formatNumber, formatOf, formatPeriod, formatShort, formatWindow, label, num,
  timeKey, titleOf } from "@/lib/charts";
import { cn } from "@/lib/utils";

// --- ECharts, loaded only when a chart needs it ---------------------------------------------------

type EChartsModule = typeof import("echarts/core");
let loading: Promise<EChartsModule> | null = null;

function loadECharts(): Promise<EChartsModule> {
  loading ??= Promise.all([import("echarts/core"), import("echarts/charts"), import("echarts/components"), import("echarts/renderers")])
    .then(([echarts, charts, components, renderers]) => {
      echarts.use([charts.LineChart, charts.BarChart, charts.PieChart, charts.TreemapChart, charts.SunburstChart, charts.FunnelChart,
        charts.BoxplotChart, charts.ScatterChart, charts.HeatmapChart, charts.RadarChart, charts.SankeyChart, charts.GraphChart,
        charts.TreeChart, charts.MapChart, charts.GaugeChart, components.GridComponent, components.TooltipComponent,
        components.LegendComponent, components.VisualMapComponent, components.CalendarComponent, components.RadarComponent,
        components.MarkLineComponent, renderers.SVGRenderer]);
      return echarts;
    });
  return loading;
}

let world: Promise<void> | null = null;
/** The world's countries, keyed by ISO numeric code, registered once and only when a map is drawn. */
function loadWorld(echarts: EChartsModule): Promise<void> {
  world ??= Promise.all([import("world-atlas/countries-110m.json"), import("topojson-client")]).then(([atlas, topojson]) => {
    const topology = (atlas as { default?: unknown }).default ?? atlas;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const shapes = topojson.feature(topology as any, (topology as any).objects.countries) as unknown as Countries;
    // Each country is named by its ISO numeric code, which is what data are matched on. Antarctica
    // takes a fifth of the map and never holds data, so it's left out.
    shapes.features = shapes.features.filter((country) => country.id !== ANTARCTICA).map((country) => ({
      ...country, properties: { ...country.properties, name: String(country.id) }, geometry: unwrapped(country.geometry) }));
    echarts.registerMap("nova-world", shapes as never);
  });
  return world;
}

const ANTARCTICA = "010";
type Ring = [number, number][];
type Geometry = { type: "Polygon"; coordinates: Ring[] } | { type: "MultiPolygon"; coordinates: Ring[][] };
type Countries = { type: "FeatureCollection"; features: { type: "Feature"; id?: string; properties: Record<string, unknown>; geometry: Geometry }[] };

/** Russia and Fiji cross the date line, and a flat map would draw a stripe across the world joining
 *  their two ends. Moving the far side's points past 180° keeps each shape in one piece. */
function unwrapped(geometry: Geometry): Geometry {
  const fix = (ring: Ring): Ring => (ring.some(([lon]) => lon > 160) && ring.some(([lon]) => lon < -160)
    ? ring.map(([lon, lat]) => [lon < 0 ? lon + 360 : lon, lat]) : ring);
  if (!geometry) return geometry;
  return geometry.type === "Polygon" ? { ...geometry, coordinates: geometry.coordinates.map(fix) }
    : { ...geometry, coordinates: geometry.coordinates.map((polygon) => polygon.map(fix)) };
}

function useTheme(): number {
  const [version, setVersion] = useState(0);
  useEffect(() => {
    const redraw = () => setVersion((value) => value + 1);
    window.addEventListener("nova-theme", redraw);
    return () => window.removeEventListener("nova-theme", redraw);
  }, []);
  return version;
}

function EChart({ spec, data, onReady }: { spec: ChartSpec; data: ChartData; onReady: () => void }) {
  const element = useRef<HTMLDivElement>(null);
  const theme = useTheme();

  useEffect(() => {
    let disposed = false;
    let cleanup: (() => void) | undefined;
    loadECharts().then(async (echarts) => {
      if (spec.viz === "world_map") await loadWorld(echarts);
      if (disposed || !element.current) return;
      const option = optionFor(spec, data, readPalette());
      const chart = echarts.init(element.current, undefined, { renderer: "svg" });
      chart.setOption({ animationDuration: 400, ...option } as never);
      chart.on("finished", onReady);
      const observer = new ResizeObserver(() => chart.resize());
      observer.observe(element.current);
      cleanup = () => { observer.disconnect(); chart.dispose(); };
    });
    return () => { disposed = true; cleanup?.(); };
  }, [spec, data, theme, onReady]);

  // A big tree gets the rows it needs and scrolls, rather than squeezing its labels on top of each other.
  const tall = spec.viz === "tree" ? treeHeight(spec, data) : 0;
  const chart = <div ref={element} className="h-full w-full" style={tall ? { minHeight: tall } : undefined} role="img" aria-label={`${spec.title}: chart`} />;
  return tall ? <div className="h-full overflow-y-auto">{chart}</div> : chart;
}

// --- Single numbers, in Cube's tile style -----------------------------------------------------------

function Delta({ current, previous }: { current: number; previous: number }) {
  if (!previous || Number.isNaN(previous) || Number.isNaN(current)) return null;
  const change = (current - previous) / Math.abs(previous);
  const Icon = change > 0.0005 ? ArrowUpRight : change < -0.0005 ? ArrowDownRight : Minus;
  // The direction is told by the arrow and the sign as well as the colour.
  const tone = change > 0.0005 ? "text-success" : change < -0.0005 ? "text-destructive" : "text-muted-foreground";
  return (
    <span className={cn("inline-flex items-center gap-0.5 text-sm font-medium tabular-nums", tone)}>
      <Icon className="size-4" aria-hidden />{change > 0 ? "+" : ""}{(change * 100).toFixed(1)}%
    </span>
  );
}

function Sparkline({ values }: { values: number[] }) {
  const finite = values.filter((v) => !Number.isNaN(v));
  if (finite.length < 2) return null;
  const min = Math.min(...finite), max = Math.max(...finite), span = max - min || 1;
  const points = values.map((v, i) => `${(i / (values.length - 1)) * 100},${28 - ((v - min) / span) * 26}`).join(" ");
  return (
    <svg viewBox="0 0 100 30" preserveAspectRatio="none" className="h-8 w-28 overflow-visible" aria-hidden>
      <polyline points={points} fill="none" stroke="var(--series-1)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

function KpiTile({ spec, data }: { spec: ChartSpec; data: ChartData }) {
  const measure = spec.measures[0];
  const format = formatOf(data.annotation, measure);
  if (spec.viz === "big_number_trend") {
    const key = timeKey(spec)!;
    const values = data.rows.map((row) => num(row[measure]));
    const last = values.at(-1) ?? NaN, previous = values.at(-2) ?? NaN;
    return (
      <div className="flex flex-wrap items-end justify-between gap-x-4 gap-y-1">
        <div>
          <p className="text-[28px] font-semibold leading-tight tracking-tight tabular-nums">{formatShort(last, format)}</p>
          <p className="mt-0.5 flex items-center gap-2 text-xs text-muted-foreground">
            <Delta current={last} previous={previous} />
            <span>{formatPeriod(data.rows.at(-1)?.[key], spec.grain)} on the {spec.grain ?? "period"} before</span>
          </p>
        </div>
        <Sparkline values={values} />
      </div>
    );
  }
  const value = num(data.rows[0]?.[measure]);
  const target = spec.target;
  const share = target ? Math.max(0, Math.min(1, value / target)) : null;
  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
      <p className="text-[28px] font-semibold leading-tight tracking-tight tabular-nums" title={formatNumber(value, format)}>{formatShort(value, format)}</p>
      {share != null && (
        <div className="min-w-32 flex-1">
          <div className="h-2 overflow-hidden rounded-full bg-muted" role="progressbar" aria-valuenow={Math.round(share * 100)} aria-valuemin={0} aria-valuemax={100}>
            <div className="h-full rounded-full bg-success" style={{ width: `${share * 100}%` }} />
          </div>
          <p className="mt-1 text-[11px] text-muted-foreground">{Math.round(share * 100)}% of {formatShort(target, format)}</p>
        </div>
      )}
    </div>
  );
}

// --- Tables -----------------------------------------------------------------------------------------

function columnsOf(spec: ChartSpec, data: ChartData): string[] {
  const time = timeKey(spec);
  const dims = [...(time ? [time] : []), ...spec.dimensions, ...(spec.pivot_columns ?? []), ...(spec.distribute_across ?? []),
    ...(spec.value_dimension ? [spec.value_dimension] : [])];
  const measures = [...spec.measures, ...(spec.line_measures ?? [])];
  const known = new Set([...dims, ...measures]);
  return [...dims, ...measures, ...Object.keys(data.rows[0] ?? {}).filter((key) => !known.has(key) && !key.startsWith(spec.time_dimension ?? "\0"))];
}

export function DataTable({ spec, data, limit = 500 }: { spec: ChartSpec; data: ChartData; limit?: number }) {
  const columns = columnsOf(spec, data);
  const measures = new Set([...spec.measures, ...(spec.line_measures ?? [])]);
  const time = timeKey(spec);
  const showTotals = spec.viz === "table" && spec.totals !== false && data.rows.length > 1;
  return (
    <div className="h-full overflow-auto">
      <table className="w-full text-xs">
        <thead className="sticky top-0 z-10 bg-card">
          <tr>{columns.map((column) => (
            <th key={column} scope="col" className={cn("border-b border-border px-2 py-1.5 font-medium text-muted-foreground", measures.has(column) ? "text-right" : "text-left")}>
              {titleOf(data.annotation, column)}
            </th>
          ))}</tr>
        </thead>
        <tbody>
          {data.rows.slice(0, limit).map((row, index) => (
            <tr key={index} className="border-b border-border last:border-0 hover:bg-muted/60">
              {columns.map((column) => (
                <td key={column} className={cn("px-2 py-1.5", measures.has(column) && "text-right tabular-nums")}>
                  {measures.has(column) ? formatNumber(row[column], formatOf(data.annotation, column))
                    : column === time ? formatPeriod(row[column], spec.grain) : label(row[column])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
        {showTotals && (
          <tfoot className="sticky bottom-0 bg-card font-medium">
            <tr>{columns.map((column, index) => (
              <td key={column} className={cn("border-t border-border px-2 py-1.5", measures.has(column) && "text-right tabular-nums")}>
                {measures.has(column) && data.annotation.measures?.[column]?.additive
                  ? formatNumber(data.rows.reduce((sum, row) => sum + (num(row[column]) || 0), 0), formatOf(data.annotation, column))
                  : index === 0 ? "Total" : ""}
              </td>
            ))}</tr>
          </tfoot>
        )}
      </table>
      {data.rows.length > limit && <p className="p-2 text-[11px] text-muted-foreground">Showing the first {limit} of {data.rows.length} rows.</p>}
    </div>
  );
}

function Pivot({ spec, data }: { spec: ChartSpec; data: ChartData }) {
  const measure = spec.measures[0];
  const format = formatOf(data.annotation, measure);
  const rowKeys = spec.dimensions, colKey = spec.pivot_columns?.[0];
  const rowName = (row: Record<string, unknown>) => rowKeys.map((k) => label(row[k])).join(" · ") || "Total";
  const rows = [...new Set(data.rows.map(rowName))];
  const cols = colKey ? [...new Set(data.rows.map((row) => label(row[colKey])))] : [titleOf(data.annotation, measure)];
  const cell = new Map<string, number>();
  for (const row of data.rows) cell.set(`${rowName(row)}\u0000${colKey ? label(row[colKey]) : cols[0]}`, num(row[measure]));
  const additive = Boolean(data.annotation.measures?.[measure]?.additive) && spec.totals !== false;
  const sum = (values: number[]) => values.reduce((total, v) => total + (Number.isNaN(v) ? 0 : v), 0);
  return (
    <div className="h-full overflow-auto">
      <table className="w-full text-xs">
        <thead className="sticky top-0 bg-card">
          <tr>
            <th scope="col" className="border-b border-border px-2 py-1.5 text-left font-medium text-muted-foreground">{rowKeys.map((k) => titleOf(data.annotation, k)).join(" · ")}</th>
            {cols.map((col) => <th key={col} scope="col" className="border-b border-border px-2 py-1.5 text-right font-medium text-muted-foreground">{col}</th>)}
            {additive && cols.length > 1 && <th scope="col" className="border-b border-border px-2 py-1.5 text-right font-medium">Total</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const values = cols.map((col) => cell.get(`${row}\u0000${col}`) ?? NaN);
            return (
              <tr key={row} className="border-b border-border last:border-0 hover:bg-muted/60">
                <th scope="row" className="px-2 py-1.5 text-left font-normal">{row}</th>
                {values.map((value, i) => <td key={cols[i]} className="px-2 py-1.5 text-right tabular-nums">{formatNumber(value, format)}</td>)}
                {additive && cols.length > 1 && <td className="px-2 py-1.5 text-right font-medium tabular-nums">{formatNumber(sum(values), format)}</td>}
              </tr>
            );
          })}
        </tbody>
        {additive && rows.length > 1 && (
          <tfoot className="bg-card font-medium">
            <tr>
              <th scope="row" className="border-t border-border px-2 py-1.5 text-left">Total</th>
              {cols.map((col) => <td key={col} className="border-t border-border px-2 py-1.5 text-right tabular-nums">{formatNumber(sum(rows.map((r) => cell.get(`${r}\u0000${col}`) ?? NaN)), format)}</td>)}
              {cols.length > 1 && <td className="border-t border-border px-2 py-1.5 text-right tabular-nums">{formatNumber(sum([...cell.values()]), format)}</td>}
            </tr>
          </tfoot>
        )}
      </table>
    </div>
  );
}

function WordCloud({ spec, data }: { spec: ChartSpec; data: ChartData }) {
  const measure = spec.measures[0], key = spec.dimensions[0];
  const words = data.rows.map((row) => ({ text: label(row[key]), value: num(row[measure]) || 0 })).sort((a, b) => b.value - a.value);
  const top = words[0]?.value || 1;
  const format = formatOf(data.annotation, measure);
  return (
    <div className="flex h-full flex-wrap content-center items-center justify-center gap-x-3 gap-y-1 overflow-hidden p-2">
      {words.map((word, i) => (
        <span key={word.text} title={`${word.text}: ${formatNumber(word.value, format)}`} className="leading-none text-foreground"
          style={{ fontSize: `${11 + 25 * Math.sqrt(word.value / top)}px`, fontWeight: i < 5 ? 600 : 400, opacity: 0.55 + 0.45 * (word.value / top) }}>
          {word.text}
        </span>
      ))}
    </div>
  );
}

// --- A chart, however it's drawn -----------------------------------------------------------------------

export function ChartBody({ spec, data, view, onReady }: { spec: ChartSpec; data: ChartData; view: "chart" | "table"; onReady: () => void }) {
  useEffect(() => {
    // Drawn in HTML, so ready as soon as it's on the page.
    if (view === "table" || (KPIS.has(spec.viz) && spec.viz !== "gauge" && spec.viz !== "bullet") || ["table", "pivot", "word_cloud"].includes(spec.viz)) onReady();
  }, [spec, view, onReady]);
  if (data.rows.length === 0) return <p className="flex h-full items-center justify-center text-xs text-muted-foreground">No data for this window.</p>;
  if (view === "table" && spec.viz !== "pivot") return <DataTable spec={spec} data={data} />;
  if (spec.viz === "big_number" || spec.viz === "big_number_trend") return <KpiTile spec={spec} data={data} />;
  if (spec.viz === "table") return <DataTable spec={spec} data={data} />;
  if (spec.viz === "pivot") return <Pivot spec={spec} data={data} />;
  if (spec.viz === "word_cloud") return <WordCloud spec={spec} data={data} />;
  return <EChart spec={spec} data={data} onReady={onReady} />;
}

export type ChartState = "loading" | "ready" | "error";

/** A chart in a card: its title, whether it's certified, the window it covers, and Chart or Table. */
export function ChartCard({ spec, data, error, className, actions }: { spec: ChartSpec; data: ChartData | null; error?: string | null;
  className?: string; actions?: React.ReactNode }) {
  const [view, setView] = useState<"chart" | "table">("chart");
  // What's been drawn: these data in this view. A chart drawn in HTML says so in its first effect,
  // before this card's own effects run, so "drawn" belongs to the data rather than being reset by them.
  const [drawnFor, setDrawnFor] = useState<{ data: ChartData; view: string } | null>(null);
  const kpi = KPIS.has(spec.viz) && spec.viz !== "gauge" && spec.viz !== "bullet";
  const height = cardHeight(spec.viz);
  // A tree's card grows to fit its rows, up to a point, so its root and labels can all be seen.
  const grown = spec.viz === "tree" && data && data.rows.length > 0 ? Math.min(TREE_CARD_MAX_PX, treeHeight(spec, data) + CARD_CHROME_PX) : null;
  const plain = kpi || spec.viz === "table" || spec.viz === "pivot";
  const ready = useCallback(() => { if (data) setDrawnFor({ data, view }); }, [data, view]);
  const drawn = drawnFor !== null && drawnFor.data === data && drawnFor.view === view;
  const state: ChartState = error ? "error" : data && drawn ? "ready" : "loading";
  const period = data ? formatWindow(data.window) : null;
  const measureTitle = data && spec.measures[0] ? titleOf(data.annotation, spec.measures[0]) : null;

  return (
    <article className={cn("flex min-w-0 flex-col rounded-xl border border-border bg-card p-4 shadow-card", height, className)}
      style={grown ? { height: Math.max(grown, TREE_CARD_MIN_PX) } : undefined}
      data-chart-state={state} data-chart-key={spec.key} aria-busy={state === "loading"}>
      <header className="mb-2 flex min-w-0 items-start gap-2">
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-[13px] font-semibold" title={spec.title}>{spec.title}</h3>
          {(spec.subtitle || period) && (
            <p className="truncate text-[11px] text-muted-foreground">{[spec.subtitle, period].filter(Boolean).join(" · ")}</p>
          )}
        </div>
        {data && <Certified certified={data.certified} className="shrink-0 text-[11px]" />}
        {!plain && data && data.rows.length > 0 && (
          <Segmented label={`${spec.title}: view`} value={view} onChange={setView} className="shrink-0"
            options={[{ value: "chart", label: <BarChart3 className="size-3.5" aria-label="Chart" /> }, { value: "table", label: <Table2 className="size-3.5" aria-label="Table" /> }]} />
        )}
        {actions}
      </header>
      <div className="min-h-0 flex-1" aria-label={measureTitle ?? undefined}>
        {error ? <p className="text-xs text-destructive" role="alert">{error}</p>
          : data ? <div className="rise h-full"><ChartBody spec={spec} data={data} view={view} onReady={ready} /></div>
          : <Skeleton kpi={kpi} />}
      </div>
      {data?.truncated && <p className="mt-1 text-[11px] text-warning">Showing the first {spec.limit} rows only.</p>}
    </article>
  );
}

/** How tall a chart's card is: numbers are short, dials a little taller, and maps, trees and
 *  tables get room for what they hold. Cards in a row stretch to the tallest. */
export function cardHeight(viz: ChartSpec["viz"]): string {
  if (viz === "big_number" || viz === "big_number_trend") return "min-h-28";
  if (viz === "gauge" || viz === "bullet") return "h-52";
  if (viz === "world_map" || viz === "tree" || viz === "table") return "h-[26rem]";
  return "h-80";
}

const CARD_CHROME_PX = 72; // the card's padding and header
const TREE_CARD_MIN_PX = 416;
const TREE_CARD_MAX_PX = 900;

export function Skeleton({ kpi }: { kpi: boolean }) {
  if (kpi) return <div className="skeleton mt-1 h-8 w-32 rounded-md" />;
  return (
    <div className="flex h-full flex-col justify-end gap-2" aria-hidden>
      <div className="flex h-full items-end gap-2">
        {[38, 62, 48, 74, 56, 84, 68, 92, 76].map((height, index) => (
          <div key={index} className="skeleton flex-1 rounded-t" style={{ height: `${height}%` }} />
        ))}
      </div>
      <div className="skeleton h-2 w-full rounded" />
    </div>
  );
}

export { countryKey };
