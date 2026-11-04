"use client";

import { useEffect, useRef, useState } from "react";

import type { ChartPreview as Preview } from "@/lib/api";
import { escapeHtml, formatAxis, formatTime, formatValue } from "@/lib/format";

const KPI_TYPES = new Set(["big_number_total", "big_number"]);
const MAX_SERIES = 8;

function colors() {
  const style = getComputedStyle(document.documentElement);
  const read = (name: string) => style.getPropertyValue(name).trim();
  return {
    series: Array.from({ length: MAX_SERIES }, (_, index) => read(`--series-${index + 1}`)),
    grid: read("--grid"), axis: read("--axis"), surface: read("--card"), text: read("--foreground"), muted: read("--muted-foreground"),
  };
}

const metricColumn = (preview: Preview) => preview.columns.at(-1) ?? "";
const isTime = (preview: Preview) => typeof preview.rows[0]?.[preview.x_axis ?? ""] === "number";

/** Long rows into one series per group. Colour follows the series name's rank by total, and
 *  anything past the eighth folds into Other: a ninth colour would be indistinguishable. */
function seriesOf(preview: Preview) {
  const metric = metricColumn(preview);
  const group = preview.groupby[0];
  const xKey = preview.x_axis ?? "";
  const rows = isTime(preview) ? [...preview.rows].sort((a, b) => Number(a[xKey]) - Number(b[xKey])) : preview.rows;
  const categories = [...new Set(rows.map((row) => row[xKey]))];
  if (!group) return { categories, series: [{ name: metric, values: categories.map((c) => Number(rows.find((r) => r[xKey] === c)?.[metric] ?? 0)) }] };

  const totals = new Map<string, number>();
  for (const row of rows) totals.set(String(row[group]), (totals.get(String(row[group])) ?? 0) + Number(row[metric] ?? 0));
  const ranked = [...totals.entries()].sort((a, b) => b[1] - a[1]).map(([name]) => name);
  const kept = ranked.slice(0, ranked.length > MAX_SERIES ? MAX_SERIES - 1 : MAX_SERIES);
  const nameOf = (value: unknown) => (kept.includes(String(value)) ? String(value) : "Other");
  const names = ranked.length > kept.length ? [...kept, "Other"] : kept;
  return {
    categories,
    series: names.map((name) => ({
      name,
      values: categories.map((c) => rows.filter((r) => r[xKey] === c && nameOf(r[group]) === name).reduce((sum, r) => sum + Number(r[metric] ?? 0), 0)),
    })),
  };
}

function tooltipHtml(title: string, rows: { name: string; value: number; color: string }[], format: string | null, palette: ReturnType<typeof colors>) {
  // Names come from the data, so they're escaped before going into the tooltip's HTML.
  const lines = rows.map((row) => `<div style="display:flex;align-items:center;gap:8px;margin-top:4px">
    <span style="width:10px;height:2px;background:${row.color};border-radius:1px"></span>
    <strong style="color:${palette.text};font-weight:600">${escapeHtml(formatValue(row.value, format))}</strong>
    <span style="color:${palette.muted}">${escapeHtml(row.name)}</span></div>`);
  return `<div style="color:${palette.muted};font-size:11px">${escapeHtml(title)}</div>${lines.join("")}`;
}

function optionFor(preview: Preview, palette: ReturnType<typeof colors>) {
  const tooltip = { backgroundColor: palette.surface, borderColor: palette.grid, borderWidth: 1, padding: [8, 10], textStyle: { fontSize: 12 }, confine: true };

  if (preview.viz === "pie") {
    const metric = metricColumn(preview);
    const label = preview.groupby[0] ?? preview.columns[0];
    const ranked = [...preview.rows].sort((a, b) => Number(b[metric]) - Number(a[metric]));
    const kept = ranked.slice(0, MAX_SERIES - 1);
    const rest = ranked.slice(MAX_SERIES - 1).reduce((sum, row) => sum + Number(row[metric] ?? 0), 0);
    const data = kept.map((row) => ({ name: String(row[label]), value: Number(row[metric]) }));
    if (rest) data.push({ name: "Other", value: rest });
    return {
      color: palette.series,
      tooltip: { ...tooltip, trigger: "item", formatter: (p: { name: string; value: number; color: string; percent: number }) =>
        tooltipHtml(`${p.percent}% of the total`, [{ name: p.name, value: p.value, color: p.color }], preview.format, palette) },
      legend: { bottom: 0, type: "scroll", icon: "roundRect", itemWidth: 10, itemHeight: 10, textStyle: { color: palette.muted, fontSize: 11 } },
      series: [{ type: "pie", radius: ["52%", "76%"], center: ["50%", "44%"], label: { show: false }, data,
        itemStyle: { borderColor: palette.surface, borderWidth: 2, borderRadius: 4 } }],
    };
  }

  const { categories, series } = seriesOf(preview);
  const time = isTime(preview);
  const labels = categories.map((c) => (time ? formatTime(Number(c), preview.time_grain) : String(c)));
  const bars = preview.viz === "echarts_timeseries_bar";
  const area = preview.viz === "echarts_area";
  return {
    color: palette.series,
    grid: { left: 4, right: 12, top: series.length > 1 ? 32 : 12, bottom: 4, containLabel: true },
    legend: series.length > 1 ? { top: 0, left: 0, type: "scroll", icon: bars ? "roundRect" : undefined, itemWidth: bars ? 10 : 14, itemHeight: bars ? 10 : 2, textStyle: { color: palette.muted, fontSize: 11 } } : undefined,
    tooltip: {
      ...tooltip, trigger: "axis", axisPointer: bars ? { type: "shadow", shadowStyle: { color: palette.grid, opacity: 0.4 } } : { type: "line", lineStyle: { color: palette.axis, width: 1, type: "solid" } },
      formatter: (points: { axisValue: string; seriesName: string; value: number; color: string }[]) =>
        tooltipHtml(points[0]?.axisValue ?? "", points.map((p) => ({ name: series.length > 1 ? p.seriesName : metricColumn(preview), value: p.value, color: p.color })), preview.format, palette),
    },
    xAxis: { type: "category", data: labels, boundaryGap: bars, axisTick: { show: false }, axisLine: { lineStyle: { color: palette.grid } },
      axisLabel: { color: palette.axis, fontSize: 11, hideOverlap: true } },
    yAxis: { type: "value", splitLine: { lineStyle: { color: palette.grid, width: 1, type: "solid" } }, axisLabel: { color: palette.axis, fontSize: 11, formatter: (value: number) => formatAxis(value, preview.format) } },
    series: series.map((entry) => bars
      ? { type: "bar", name: entry.name, data: entry.values, barMaxWidth: 24, stack: series.length > 1 ? "total" : undefined, itemStyle: { borderRadius: [4, 4, 0, 0] } }
      : { type: "line", name: entry.name, data: entry.values, smooth: false, showSymbol: false, symbolSize: 8, lineStyle: { width: 2, cap: "round", join: "round" },
          stack: area && series.length > 1 ? "total" : undefined, areaStyle: area ? { opacity: 0.1 } : undefined }),
  };
}

function Plot({ preview }: { preview: Preview }) {
  const element = useRef<HTMLDivElement>(null);
  const [theme, setTheme] = useState(0);

  useEffect(() => {
    const redraw = () => setTheme((value) => value + 1);
    window.addEventListener("nova-theme", redraw);
    return () => window.removeEventListener("nova-theme", redraw);
  }, []);

  useEffect(() => {
    let disposed = false;
    let cleanup: (() => void) | undefined;
    // Loaded on demand: charting code isn't needed until the first chart arrives.
    Promise.all([import("echarts/core"), import("echarts/charts"), import("echarts/components"), import("echarts/renderers")])
      .then(([echarts, charts, components, renderers]) => {
        if (disposed || !element.current) return;
        echarts.use([charts.LineChart, charts.BarChart, charts.PieChart, components.GridComponent, components.TooltipComponent,
          components.LegendComponent, renderers.SVGRenderer]);
        const chart = echarts.init(element.current, undefined, { renderer: "svg" });
        chart.setOption({ ...optionFor(preview, colors()), animationDuration: 500 } as never);
        const observer = new ResizeObserver(() => chart.resize());
        observer.observe(element.current);
        cleanup = () => { observer.disconnect(); chart.dispose(); };
      });
    return () => { disposed = true; cleanup?.(); };
  }, [preview, theme]);

  return <div ref={element} className="h-full w-full" role="img" aria-label={`${preview.title} chart`} />;
}

function PreviewTable({ preview }: { preview: Preview }) {
  return (
    <div className="h-full overflow-auto">
      <table className="w-full text-xs">
        <thead className="sticky top-0 bg-card">
          <tr>{preview.columns.map((column) => <th key={column} className="border-b border-border px-2 py-1.5 text-left font-medium text-muted-foreground">{column}</th>)}</tr>
        </thead>
        <tbody>
          {preview.rows.slice(0, 50).map((row, index) => (
            <tr key={index} className="border-b border-border last:border-0">
              {preview.columns.map((column) => {
                const value = row[column];
                const numeric = typeof value === "number";
                return <td key={column} className={numeric ? "px-2 py-1.5 text-right tabular-nums" : "px-2 py-1.5"}>{numeric ? formatValue(value, preview.format) : String(value ?? "")}</td>;
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ChartPreview({ preview }: { preview: Preview }) {
  if (KPI_TYPES.has(preview.viz)) {
    return <p className="truncate text-[28px] font-semibold tracking-tight">{formatValue(preview.rows[0]?.[metricColumn(preview)], preview.format)}</p>;
  }
  if (preview.viz === "table") return <PreviewTable preview={preview} />;
  return <Plot preview={preview} />;
}

export const isKpi = (viz: string) => KPI_TYPES.has(viz);
