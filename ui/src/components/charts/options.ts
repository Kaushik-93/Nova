/* ECharts options for each kind of chart, from a chart's spec and its rows from Cube.
 *
 * The rules come from the data-visualisation guidance this UI follows: categorical colours in a fixed
 * order, never cycled (a ninth series folds into Other); one axis per chart; thin marks, 4px rounded
 * data ends, a 2px surface gap between filled segments; recessive grid and axes; a legend for two or
 * more series; a tooltip on every chart; text in text colours, never in a series colour. */

import { COUNTRIES } from "@/lib/countries";
import { type ChartData, type ChartSpec, type Format, formatNumber, formatOf, formatPeriod, formatShort, label, num,
  timeKey, timeOnX, titleOf } from "@/lib/charts";
import { escapeHtml } from "@/lib/format";

export type Palette = {
  series: string[]; sequential: string[]; grid: string; axis: string; surface: string; text: string; muted: string;
  up: string; down: string; neutral: string;
};

type Row = Record<string, unknown>;
type Option = Record<string, unknown>;
const MAX_SERIES = 8;
const OTHER = "Other";

export function readPalette(): Palette {
  const style = getComputedStyle(document.documentElement);
  const read = (name: string) => style.getPropertyValue(name).trim();
  return {
    series: Array.from({ length: MAX_SERIES }, (_, i) => read(`--series-${i + 1}`)),
    sequential: Array.from({ length: 7 }, (_, i) => read(`--seq-${i + 1}`)),
    grid: read("--grid"), axis: read("--axis"), surface: read("--card"), text: read("--foreground"), muted: read("--muted-foreground"),
    up: read("--series-1"), down: read("--series-8"), neutral: read("--axis"),
  };
}

// --- Shared pieces --------------------------------------------------------------------------------

function tooltip(palette: Palette, extra: Option = {}): Option {
  return { backgroundColor: palette.surface, borderColor: palette.grid, borderWidth: 1, padding: [8, 10],
    textStyle: { fontSize: 12, color: palette.text }, confine: true, ...extra };
}

/** A tooltip's body: a heading, then one line per value, with a small swatch carrying identity. */
function tipHtml(heading: string, lines: { name: string; value: string; color?: string }[], palette: Palette): string {
  const rows = lines.map((line) => `<div style="display:flex;align-items:center;gap:8px;margin-top:4px">
    ${line.color ? `<span style="width:10px;height:10px;border-radius:2px;background:${line.color}"></span>` : ""}
    <strong style="color:${palette.text};font-weight:600">${escapeHtml(line.value)}</strong>
    <span style="color:${palette.muted}">${escapeHtml(line.name)}</span></div>`);
  return `<div style="color:${palette.muted};font-size:11px">${escapeHtml(heading)}</div>${rows.join("")}`;
}

function legend(palette: Palette, show: boolean, extra: Option = {}): Option | undefined {
  if (!show) return undefined;
  return { top: 0, left: 0, type: "scroll", icon: "roundRect", itemWidth: 10, itemHeight: 10, itemGap: 14,
    textStyle: { color: palette.muted, fontSize: 11 }, pageTextStyle: { color: palette.muted }, ...extra };
}

function axes(palette: Palette, format: Format, categories: string[], horizontal = false): Option {
  const category = { type: "category", data: categories, axisTick: { show: false }, axisLine: { lineStyle: { color: palette.grid } },
    axisLabel: { color: palette.axis, fontSize: 11, hideOverlap: true, width: 120, overflow: "truncate" } };
  const value = { type: "value", splitLine: { lineStyle: { color: palette.grid, width: 1 } },
    axisLabel: { color: palette.axis, fontSize: 11, formatter: (v: number) => formatShort(v, format) } };
  return horizontal ? { xAxis: value, yAxis: { ...category, inverse: true } } : { xAxis: category, yAxis: value };
}

function grid(top: number): Option {
  return { left: 4, right: 16, top, bottom: 4, containLabel: true };
}

/** Values of a dimension in the order they first appear: the query already sorted them. */
function distinct(rows: Row[], key: string): string[] {
  return [...new Set(rows.map((row) => label(row[key])))];
}

/** Groups into at most eight series, by name, so a colour follows its entity; the rest are Other. */
function groupNames(rows: Row[], key: string, measure: string): { names: string[]; nameOf: (value: unknown) => string } {
  const totals = new Map<string, number>();
  for (const row of rows) totals.set(label(row[key]), (totals.get(label(row[key])) ?? 0) + (num(row[measure]) || 0));
  const ranked = [...totals.entries()].sort((a, b) => b[1] - a[1]).map(([name]) => name);
  const kept = new Set(ranked.length > MAX_SERIES ? ranked.slice(0, MAX_SERIES - 1) : ranked);
  const names = [...kept].sort((a, b) => a.localeCompare(b));
  if (ranked.length > kept.size) names.push(OTHER);
  return { names, nameOf: (value) => (kept.has(label(value)) ? label(value) : OTHER) };
}

type Series = { name: string; values: number[]; measure: string };

/** The x categories and the series for any chart with something along the bottom. */
function seriesOf(spec: ChartSpec, data: ChartData, measures: string[]): { categories: string[]; series: Series[]; xKey: string } {
  const time = timeKey(spec);
  const xKey = time ?? spec.dimensions[0];
  const groupKey = time ? spec.dimensions[0] : spec.dimensions[1];
  const rows = data.rows;
  const raw = distinct(rows, xKey);
  const categories = time ? raw.map((value) => formatPeriod(value, spec.viz === "calendar" ? "day" : spec.grain)) : raw;
  const index = new Map(raw.map((value, i) => [value, i]));
  if (groupKey && measures.length === 1) {
    const measure = measures[0];
    const { names, nameOf } = groupNames(rows, groupKey, measure);
    const series = names.map((name) => ({ name, measure, values: raw.map(() => 0) }));
    const byName = new Map(series.map((entry) => [entry.name, entry]));
    for (const row of rows) byName.get(nameOf(row[groupKey]))!.values[index.get(label(row[xKey]))!] += num(row[measure]) || 0;
    return { categories, series, xKey };
  }
  const series = measures.map((measure) => {
    const values = raw.map(() => NaN);
    for (const row of rows) values[index.get(label(row[xKey]))!] = num(row[measure]);
    return { name: titleOf(data.annotation, measure), measure, values };
  });
  return { categories, series, xKey };
}

function percentOf(series: Series[]): Series[] {
  const totals = series[0]?.values.map((_, i) => series.reduce((sum, s) => sum + (s.values[i] || 0), 0)) ?? [];
  return series.map((s) => ({ ...s, values: s.values.map((v, i) => (totals[i] ? (v || 0) / totals[i] : 0)) }));
}

function axisTooltip(palette: Palette, formatFor: (series: string) => Format, bars: boolean): Option {
  return tooltip(palette, {
    trigger: "axis",
    axisPointer: bars ? { type: "shadow", shadowStyle: { color: palette.grid, opacity: 0.5 } } : { type: "line", lineStyle: { color: palette.axis, width: 1 } },
    formatter: (points: { axisValue: string; seriesName: string; value: number; color: string }[]) =>
      tipHtml(points[0]?.axisValue ?? "", points.filter((p) => p.value != null && !Number.isNaN(p.value))
        .map((p) => ({ name: p.seriesName, value: formatNumber(p.value, formatFor(p.seriesName)), color: p.color })), palette),
  });
}

// --- Over time and across categories ---------------------------------------------------------------

function cartesian(spec: ChartSpec, data: ChartData, palette: Palette): Option {
  const combo = spec.viz === "combo";
  const measures = combo ? [...spec.measures, ...(spec.line_measures ?? [])] : spec.measures;
  let { categories, series } = seriesOf(spec, data, measures);
  const percent = spec.stacking === "percent";
  if (percent) series = percentOf(series);
  const format: Format = percent ? "percent" : formatOf(data.annotation, measures[0]);
  const bars = spec.viz === "bar" || combo;
  const stacked = spec.stacking && spec.stacking !== "none" && series.length > 1;
  const lineKind = { smooth_line: { smooth: 0.35 }, step_line: { step: "middle" } }[spec.viz as string] ?? {};
  const horizontal = Boolean(spec.horizontal) && bars && !combo;
  const many = series.length > 1;

  const built = series.map((entry, i) => {
    const asBar = spec.viz === "bar" || (combo && spec.measures.includes(entry.measure));
    if (asBar) {
      const last = i === series.length - 1 || !stacked;
      return { type: "bar", name: entry.name, data: entry.values, stack: stacked ? "total" : undefined, barMaxWidth: 28,
        itemStyle: { borderColor: palette.surface, borderWidth: stacked ? 1 : 0,
          borderRadius: last ? (horizontal ? [0, 4, 4, 0] : [4, 4, 0, 0]) : 0 } };
    }
    if (spec.viz === "dots") {
      return { type: "scatter", name: entry.name, data: entry.values, symbolSize: 8,
        itemStyle: { borderColor: palette.surface, borderWidth: 2 } };
    }
    return { type: "line", name: entry.name, data: entry.values, showSymbol: false, symbol: "circle", symbolSize: 8,
      lineStyle: { width: 2, cap: "round", join: "round" }, connectNulls: false, stack: spec.viz === "area" && stacked ? "total" : undefined,
      areaStyle: spec.viz === "area" ? { opacity: stacked ? 0.85 : 0.15 } : undefined, emphasis: { focus: "series" }, ...lineKind };
  });
  const formats = new Map(series.map((s) => [s.name, percent ? "percent" as Format : formatOf(data.annotation, s.measure)]));
  return {
    color: palette.series,
    grid: grid(many ? 32 : 12),
    legend: legend(palette, many),
    tooltip: axisTooltip(palette, (name) => formats.get(name) ?? format, bars || spec.viz === "dots"),
    ...axes(palette, format, categories, horizontal),
    series: built,
  };
}

function waterfall(spec: ChartSpec, data: ChartData, palette: Palette): Option {
  const { categories, series } = seriesOf(spec, data, spec.measures);
  const steps = series[0]?.values.map((v) => v || 0) ?? [];
  const format = formatOf(data.annotation, spec.measures[0]);
  let running = 0;
  const base: number[] = [], up: (number | string)[] = [], down: (number | string)[] = [];
  for (const step of steps) {
    base.push(step >= 0 ? running : running + step);
    up.push(step >= 0 ? step : "-");
    down.push(step < 0 ? -step : "-");
    running += step;
  }
  const total = running;
  const labels = [...categories, "Total"];
  const bar = (name: string, values: (number | string)[], color: string) => ({
    type: "bar", name, stack: "w", data: values, barMaxWidth: 36, itemStyle: { color, borderRadius: 4 } });
  return {
    grid: grid(32),
    legend: legend(palette, true, { data: ["Increase", "Decrease", "Total"] }),
    tooltip: tooltip(palette, { trigger: "axis", axisPointer: { type: "shadow", shadowStyle: { color: palette.grid, opacity: 0.5 } },
      formatter: (points: { dataIndex: number }[]) => {
        const i = points[0]?.dataIndex ?? 0;
        const value = i < steps.length ? steps[i] : total;
        return tipHtml(labels[i], [{ name: i < steps.length ? "Change" : "Total", value: formatNumber(value, format) }], palette);
      } }),
    ...axes(palette, format, labels),
    series: [
      { type: "bar", stack: "w", data: [...base, 0], itemStyle: { color: "transparent" }, silent: true, tooltip: { show: false } },
      bar("Increase", [...up, "-"], palette.up),
      bar("Decrease", [...down, "-"], palette.down),
      bar("Total", [...steps.map(() => "-"), total], palette.neutral),
    ],
  };
}

// --- Parts of a whole ------------------------------------------------------------------------------

function slices(data: ChartData, key: string, measure: string): { name: string; value: number }[] {
  const ranked = data.rows.map((row) => ({ name: label(row[key]), value: num(row[measure]) || 0 })).sort((a, b) => b.value - a.value);
  if (ranked.length <= MAX_SERIES) return ranked;
  const kept = ranked.slice(0, MAX_SERIES - 1);
  return [...kept, { name: OTHER, value: ranked.slice(MAX_SERIES - 1).reduce((sum, item) => sum + item.value, 0) }];
}

function pie(spec: ChartSpec, data: ChartData, palette: Palette): Option {
  const measure = spec.measures[0];
  const format = formatOf(data.annotation, measure);
  return {
    color: palette.series,
    legend: legend(palette, true, { top: undefined, bottom: 0 }),
    tooltip: tooltip(palette, { trigger: "item", formatter: (p: { name: string; value: number; percent: number; color: string }) =>
      tipHtml(`${p.percent}% of the total`, [{ name: p.name, value: formatNumber(p.value, format), color: p.color }], palette) }),
    series: [{ type: "pie", radius: spec.donut === false ? ["0%", "70%"] : ["48%", "72%"], center: ["50%", "45%"],
      data: slices(data, spec.dimensions[0], measure), label: { show: false }, avoidLabelOverlap: true,
      itemStyle: { borderColor: palette.surface, borderWidth: 2, borderRadius: spec.donut === false ? 0 : 4 } }],
  };
}

type Node = { name: string; value: number; children?: Node[] };

function hierarchy(data: ChartData, keys: string[], measure: string): Node[] {
  const root: Node = { name: "", value: 0, children: [] };
  for (const row of data.rows) {
    const value = num(row[measure]) || 0;
    let node = root;
    for (const key of keys) {
      node.children ??= [];
      let child = node.children.find((c) => c.name === label(row[key]));
      if (!child) node.children.push(child = { name: label(row[key]), value: 0 });
      child.value += value;
      node = child;
    }
  }
  return root.children ?? [];
}

function treemapOrSunburst(spec: ChartSpec, data: ChartData, palette: Palette): Option {
  const measure = spec.measures[0];
  const format = formatOf(data.annotation, measure);
  const nodes = hierarchy(data, spec.dimensions, measure).sort((a, b) => b.value - a.value);
  // The top level carries the colour, in the fixed order; past eight it repeats nothing, it goes grey.
  const coloured = nodes.map((node, i) => ({ ...node, itemStyle: { color: i < MAX_SERIES ? palette.series[i] : palette.neutral } }));
  const tip = tooltip(palette, { formatter: (p: { treePathInfo?: { name: string }[]; name: string; value: number }) =>
    tipHtml((p.treePathInfo ?? []).map((n) => n.name).filter(Boolean).join(" / ") || p.name, [{ name: titleOf(data.annotation, measure), value: formatNumber(p.value, format) }], palette) });
  if (spec.viz === "treemap") {
    return { tooltip: tip, series: [{ type: "treemap", data: coloured, roam: false, nodeClick: false, breadcrumb: { show: false },
      top: 0, left: 0, right: 0, bottom: 0, label: { color: "#fff", fontSize: 11, overflow: "truncate" },
      upperLabel: { show: spec.dimensions.length > 1, height: 18, color: "#fff", fontSize: 11 },
      itemStyle: { borderColor: palette.surface, borderWidth: 2, gapWidth: 2 },
      levels: [{ itemStyle: { borderColor: palette.surface, borderWidth: 2, gapWidth: 2 } }, { colorSaturation: [0.35, 0.6], itemStyle: { gapWidth: 1, borderColorSaturation: 0.6 } }] }] };
  }
  return { tooltip: tip, series: [{ type: "sunburst", data: coloured, radius: ["12%", "92%"], sort: undefined,
    itemStyle: { borderColor: palette.surface, borderWidth: 2 }, label: { color: "#fff", fontSize: 10, minAngle: 12, overflow: "truncate" } }] };
}

function funnel(spec: ChartSpec, data: ChartData, palette: Palette): Option {
  const measure = spec.measures[0];
  const format = formatOf(data.annotation, measure);
  const stages = data.rows.map((row) => ({ name: label(row[spec.dimensions[0]]), value: num(row[measure]) || 0 }));
  const top = Math.max(...stages.map((s) => s.value), 1);
  return {
    color: palette.series,
    tooltip: tooltip(palette, { trigger: "item", formatter: (p: { name: string; value: number; color: string }) =>
      tipHtml(p.name, [{ name: `${Math.round((p.value / top) * 100)}% of the first stage`, value: formatNumber(p.value, format), color: p.color }], palette) }),
    series: [{ type: "funnel", sort: "descending", gap: 2, left: "10%", right: "10%", top: 8, bottom: 8, data: stages,
      label: { color: palette.text, fontSize: 11, formatter: (p: { name: string; value: number }) => `${p.name}  ${formatShort(p.value, format)}` },
      itemStyle: { borderColor: palette.surface, borderWidth: 2 } }],
  };
}

// --- Distributions and relationships ----------------------------------------------------------------

function histogram(spec: ChartSpec, data: ChartData, palette: Palette): Option {
  const valueKey = spec.value_dimension!;
  const countKey = spec.measures[0];
  const points = data.rows.map((row) => ({ x: num(row[valueKey]), n: num(row[countKey]) || 0 })).filter((p) => !Number.isNaN(p.x));
  const min = Math.min(...points.map((p) => p.x)), max = Math.max(...points.map((p) => p.x));
  const bins = Math.max(2, Math.min(spec.bins ?? 20, 100));
  const width = (max - min) / bins || 1;
  const counts = Array.from({ length: bins }, () => 0);
  for (const p of points) counts[Math.min(bins - 1, Math.floor((p.x - min) / width))] += p.n;
  const labels = counts.map((_, i) => `${formatShort(min + i * width)}–${formatShort(min + (i + 1) * width)}`);
  return {
    color: [palette.series[0]],
    grid: grid(12),
    tooltip: tooltip(palette, { trigger: "axis", axisPointer: { type: "shadow", shadowStyle: { color: palette.grid, opacity: 0.5 } },
      formatter: (ps: { name: string; value: number }[]) => tipHtml(ps[0].name, [{ name: titleOf(data.annotation, countKey), value: formatNumber(ps[0].value) }], palette) }),
    ...axes(palette, "number", labels),
    series: [{ type: "bar", data: counts, barCategoryGap: 1, itemStyle: { borderRadius: [4, 4, 0, 0] } }],
  };
}

function quartiles(values: number[]): [number, number, number, number, number] {
  const sorted = [...values].sort((a, b) => a - b);
  const at = (q: number) => {
    const position = (sorted.length - 1) * q;
    const low = Math.floor(position), high = Math.ceil(position);
    return sorted[low] + (sorted[high] - sorted[low]) * (position - low);
  };
  return [sorted[0], at(0.25), at(0.5), at(0.75), sorted.at(-1)!];
}

function boxPlot(spec: ChartSpec, data: ChartData, palette: Palette): Option {
  const group = spec.dimensions[0], measure = spec.measures[0];
  const format = formatOf(data.annotation, measure);
  const groups = distinct(data.rows, group);
  const stats = groups.map((name) => quartiles(data.rows.filter((row) => label(row[group]) === name).map((row) => num(row[measure])).filter((v) => !Number.isNaN(v))));
  return {
    color: [palette.series[0]],
    grid: grid(12),
    tooltip: tooltip(palette, { trigger: "item", formatter: (p: { name: string; data: number[] }) => tipHtml(p.name,
      ["Highest", "Upper quartile", "Median", "Lower quartile", "Lowest"].map((name, i) => ({ name, value: formatNumber(p.data[5 - i], format) })), palette) }),
    ...axes(palette, format, groups),
    series: [{ type: "boxplot", data: stats, itemStyle: { color: palette.surface, borderColor: palette.series[0], borderWidth: 2 }, boxWidth: [8, 36] }],
  };
}

function scatter(spec: ChartSpec, data: ChartData, palette: Palette): Option {
  const [xm, ym, sm] = spec.measures;
  const [pointKey, colourKey] = spec.dimensions;
  const formats = [xm, ym, sm].map((m) => formatOf(data.annotation, m));
  const sizes = sm ? data.rows.map((row) => num(row[sm]) || 0) : [];
  const biggest = Math.max(...sizes, 1);
  const groups = colourKey ? groupNames(data.rows, colourKey, ym) : null;
  const names = groups?.names ?? [titleOf(data.annotation, ym)];
  return {
    color: palette.series,
    grid: grid(names.length > 1 ? 32 : 12),
    legend: legend(palette, names.length > 1),
    tooltip: tooltip(palette, { trigger: "item", formatter: (p: { data: [number, number, number, string]; color: string }) => tipHtml(p.data[3], [
      { name: titleOf(data.annotation, xm), value: formatNumber(p.data[0], formats[0]), color: p.color },
      { name: titleOf(data.annotation, ym), value: formatNumber(p.data[1], formats[1]) },
      ...(sm ? [{ name: titleOf(data.annotation, sm), value: formatNumber(p.data[2], formats[2]) }] : [])], palette) }),
    xAxis: { type: "value", scale: true, name: titleOf(data.annotation, xm), nameLocation: "middle", nameGap: 26, nameTextStyle: { color: palette.muted, fontSize: 11 },
      splitLine: { lineStyle: { color: palette.grid } }, axisLabel: { color: palette.axis, fontSize: 11, formatter: (v: number) => formatShort(v, formats[0]) } },
    yAxis: { type: "value", scale: true, splitLine: { lineStyle: { color: palette.grid } }, axisLabel: { color: palette.axis, fontSize: 11, formatter: (v: number) => formatShort(v, formats[1]) } },
    series: names.map((name) => ({
      type: "scatter", name,
      data: data.rows.filter((row) => !groups || groups.nameOf(row[colourKey!]) === name)
        .map((row) => [num(row[xm]), num(row[ym]), sm ? num(row[sm]) : 0, label(row[pointKey])]),
      symbolSize: (value: number[]) => (sm ? 8 + 28 * Math.sqrt((value[2] || 0) / biggest) : 10),
      itemStyle: { borderColor: palette.surface, borderWidth: 2, opacity: 0.9 },
    })),
  };
}

function heatmap(spec: ChartSpec, data: ChartData, palette: Palette): Option {
  const measure = spec.measures[0];
  const format = formatOf(data.annotation, measure);
  const time = timeKey(spec);
  const xKey = time ?? spec.dimensions[0];
  const yKey = time ? spec.dimensions[0] : spec.dimensions[1];
  const xs = distinct(data.rows, xKey), ys = distinct(data.rows, yKey);
  const values = data.rows.map((row) => [xs.indexOf(label(row[xKey])), ys.indexOf(label(row[yKey])), num(row[measure]) || 0]);
  const top = Math.max(...values.map((v) => v[2]), 0);
  return {
    grid: { ...grid(8), bottom: 40 },
    tooltip: tooltip(palette, { formatter: (p: { data: number[] }) => tipHtml(`${time ? formatPeriod(xs[p.data[0]], spec.grain) : xs[p.data[0]]} · ${ys[p.data[1]]}`,
      [{ name: titleOf(data.annotation, measure), value: formatNumber(p.data[2], format) }], palette) }),
    xAxis: { type: "category", data: time ? xs.map((x) => formatPeriod(x, spec.grain)) : xs, splitArea: { show: false }, axisTick: { show: false },
      axisLine: { lineStyle: { color: palette.grid } }, axisLabel: { color: palette.axis, fontSize: 11, hideOverlap: true } },
    yAxis: { type: "category", data: ys, axisTick: { show: false }, axisLine: { show: false }, axisLabel: { color: palette.axis, fontSize: 11, width: 110, overflow: "truncate" } },
    visualMap: { min: 0, max: top || 1, calculable: false, orient: "horizontal", left: "center", bottom: 0, itemHeight: 120, itemWidth: 10,
      inRange: { color: palette.sequential }, textStyle: { color: palette.muted, fontSize: 10 }, formatter: (v: number) => formatShort(v, format) },
    series: [{ type: "heatmap", data: values, itemStyle: { borderColor: palette.surface, borderWidth: 2, borderRadius: 2 } }],
  };
}

function calendar(spec: ChartSpec, data: ChartData, palette: Palette): Option {
  const measure = spec.measures[0];
  const format = formatOf(data.annotation, measure);
  const key = timeKey(spec)!;
  const days = data.rows.map((row) => [String(row[key]).slice(0, 10), num(row[measure]) || 0] as [string, number]);
  const range = data.window ? [data.window[0].startsWith("1900") ? days[0]?.[0] : data.window[0], data.window[1]] : [days[0]?.[0], days.at(-1)?.[0]];
  const top = Math.max(...days.map((d) => d[1]), 0);
  return {
    tooltip: tooltip(palette, { formatter: (p: { data: [string, number] }) => tipHtml(formatPeriod(p.data[0], "day"), [{ name: titleOf(data.annotation, measure), value: formatNumber(p.data[1], format) }], palette) }),
    visualMap: { min: 0, max: top || 1, orient: "horizontal", left: "center", bottom: 0, itemHeight: 120, itemWidth: 10,
      inRange: { color: palette.sequential }, textStyle: { color: palette.muted, fontSize: 10 }, formatter: (v: number) => formatShort(v, format) },
    calendar: { range, top: 24, left: 36, right: 12, bottom: 40, cellSize: ["auto", 13], splitLine: { show: false },
      itemStyle: { color: palette.surface, borderColor: palette.surface, borderWidth: 2 },
      yearLabel: { show: false }, dayLabel: { color: palette.axis, fontSize: 10, firstDay: 1, nameMap: ["S", "M", "T", "W", "T", "F", "S"] },
      monthLabel: { color: palette.axis, fontSize: 10 } },
    series: [{ type: "heatmap", coordinateSystem: "calendar", data: days }],
  };
}

function radar(spec: ChartSpec, data: ChartData, palette: Palette): Option {
  const measures = spec.measures;
  const formats = measures.map((m) => formatOf(data.annotation, m));
  const key = spec.dimensions[0];
  const items = key ? data.rows.slice(0, MAX_SERIES) : data.rows.slice(0, 1);
  const tops = measures.map((m) => Math.max(...data.rows.map((row) => num(row[m]) || 0), 0) * 1.1 || 1);
  return {
    color: palette.series,
    legend: legend(palette, items.length > 1, { top: undefined, bottom: 0 }),
    tooltip: tooltip(palette, { trigger: "item", formatter: (p: { name: string; value: number[]; color: string }) =>
      tipHtml(p.name, measures.map((m, i) => ({ name: titleOf(data.annotation, m), value: formatNumber(p.value[i], formats[i]) })), palette) }),
    radar: { indicator: measures.map((m, i) => ({ name: titleOf(data.annotation, m), max: tops[i] })), radius: "62%", center: ["50%", "48%"],
      splitLine: { lineStyle: { color: palette.grid } }, splitArea: { show: false }, axisLine: { lineStyle: { color: palette.grid } },
      axisName: { color: palette.muted, fontSize: 11 } },
    series: [{ type: "radar", symbolSize: 6, lineStyle: { width: 2 }, areaStyle: { opacity: 0.08 },
      data: items.map((row) => ({ name: key ? label(row[key]) : "Total", value: measures.map((m) => num(row[m]) || 0) })) }],
  };
}

// --- Flows, structures and places -------------------------------------------------------------------

function flows(spec: ChartSpec, data: ChartData, palette: Palette): Option {
  const measure = spec.measures[0];
  const format = formatOf(data.annotation, measure);
  const [fromKey, toKey] = spec.dimensions;
  // The two sides can share names, and a flow must not loop, so the sides are told apart.
  const shared = new Set(distinct(data.rows, fromKey).filter((name) => distinct(data.rows, toKey).includes(name)));
  const to = (name: string) => (shared.has(name) && spec.viz === "sankey" ? `${name} ` : name);
  const links = data.rows.map((row) => ({ source: label(row[fromKey]), target: to(label(row[toKey])), value: num(row[measure]) || 0 })).filter((l) => l.value > 0);
  const names = [...new Set(links.flatMap((l) => [l.source, l.target]))];
  const colour = (i: number) => palette.series[i % MAX_SERIES];
  const tip = tooltip(palette, { trigger: "item", formatter: (p: { dataType: string; data: { source?: string; target?: string; value?: number }; name: string; value: number }) =>
    p.dataType === "edge" ? tipHtml(`${p.data.source} → ${String(p.data.target).trim()}`, [{ name: titleOf(data.annotation, measure), value: formatNumber(p.data.value, format) }], palette)
      : tipHtml(String(p.name).trim(), [{ name: titleOf(data.annotation, measure), value: formatNumber(p.value, format) }], palette) });
  if (spec.viz === "sankey") {
    return { tooltip: tip, series: [{ type: "sankey", left: 8, right: 96, top: 8, bottom: 8, nodeGap: 8, nodeWidth: 12, draggable: false,
      data: names.map((name, i) => ({ name, itemStyle: { color: colour(i), borderColor: palette.surface } })), links,
      lineStyle: { color: "gradient", opacity: 0.3, curveness: 0.5 }, label: { color: palette.text, fontSize: 11, formatter: (p: { name: string }) => p.name.trim() },
      emphasis: { focus: "adjacency" } }] };
  }
  const totals = new Map<string, number>();
  for (const l of links) { totals.set(l.source, (totals.get(l.source) ?? 0) + l.value); totals.set(l.target, (totals.get(l.target) ?? 0) + l.value); }
  const biggest = Math.max(...totals.values(), 1), heaviest = Math.max(...links.map((l) => l.value), 1);
  return { tooltip: tip, series: [{ type: "graph", layout: "force", roam: true, force: { repulsion: 70, gravity: 0.25, edgeLength: [24, 70] }, labelLayout: { hideOverlap: true }, top: 24, bottom: 24, left: 24, right: 60,
    data: names.map((name, i) => ({ name, value: totals.get(name), symbolSize: 8 + 22 * Math.sqrt((totals.get(name) ?? 0) / biggest),
      itemStyle: { color: colour(i), borderColor: palette.surface, borderWidth: 2 } })),
    links: links.map((l) => ({ ...l, lineStyle: { width: 1 + 4 * (l.value / heaviest), color: palette.axis, opacity: 0.45, curveness: 0.15 } })),
    label: { show: names.length <= 30, position: "right", color: palette.text, fontSize: 10 }, edgeSymbol: ["none", "arrow"], edgeSymbolSize: 6 }] };
}

type TreeNode = { name: string; children: TreeNode[]; id: string };
const TREE_ROW_PX = 18;
const TREE_MAX_ROWS = 40;

/** The tree, and how many levels it opens at: as many as fit about 40 rows, so a big organisation
 *  opens at its top levels and the rest is a click away. */
function buildTree(spec: ChartSpec, data: ChartData): { root: TreeNode | undefined; depth: number; rows: number } {
  const [idKey, parentKey, nameKey] = spec.dimensions;
  const nodes = new Map<string, TreeNode>();
  for (const row of data.rows) nodes.set(label(row[idKey]), { id: label(row[idKey]), name: label(row[nameKey ?? idKey]), children: [] });
  for (const row of data.rows) {
    const parent = row[parentKey] == null ? undefined : nodes.get(label(row[parentKey]));
    const node = nodes.get(label(row[idKey]))!;
    if (parent && parent !== node && !parent.children.includes(node)) parent.children.push(node);
  }
  const root = (spec.tree_root ? nodes.get(spec.tree_root) : undefined) ?? [...nodes.values()].find((n) => !data.rows.some((r) => label(r[idKey]) === n.id && r[parentKey] != null));
  // The rows a tree opened `depth` levels down shows: its leaves, counting a closed branch as one.
  const visible = (node: TreeNode, depth: number, seen = new Set<TreeNode>()): number => {
    if (seen.has(node)) return 0;
    seen.add(node);
    return depth === 0 || node.children.length === 0 ? 1 : node.children.reduce((sum, child) => sum + visible(child, depth - 1, seen), 0);
  };
  let depth = 1;
  while (root && depth < 4 && visible(root, depth + 1) <= TREE_MAX_ROWS && visible(root, depth + 1) > visible(root, depth)) depth += 1;
  return { root, depth, rows: root ? visible(root, depth) : 0 };
}

/** The height a tree needs for its labels not to overlap, in pixels. */
export function treeHeight(spec: ChartSpec, data: ChartData): number {
  return buildTree(spec, data).rows * TREE_ROW_PX + 32;
}

function tree(spec: ChartSpec, data: ChartData, palette: Palette): Option {
  const { root, depth } = buildTree(spec, data);
  return {
    tooltip: tooltip(palette, { trigger: "item", formatter: (p: { name: string }) => escapeHtml(p.name) }),
    // The root's name sits to its left, so the tree starts far enough in for it to fit.
    series: [{ type: "tree", data: root ? [root] : [], top: 16, left: Math.min(200, 16 + 6.5 * (root?.name.length ?? 8)), bottom: 16, right: 140, symbolSize: 7, initialTreeDepth: depth,
      itemStyle: { color: palette.series[0], borderColor: palette.surface }, lineStyle: { color: palette.grid, width: 1.5 },
      label: { color: palette.text, fontSize: 11, position: "left", align: "right" }, leaves: { label: { position: "right", align: "left" } },
      expandAndCollapse: true, animationDuration: 300 }],
  };
}

export function countryKey(code: string, form: ChartSpec["country_codes"]): string | undefined {
  const index = form === "cca3" ? 1 : form === "name" ? 3 : 0;
  const wanted = code.trim().toLowerCase();
  return COUNTRIES.find((entry) => entry[index].toLowerCase() === wanted)?.[2];
}

function worldMap(spec: ChartSpec, data: ChartData, palette: Palette): Option {
  const measure = spec.measures[0];
  const format = formatOf(data.annotation, measure);
  const values = data.rows.map((row) => ({ name: countryKey(label(row[spec.dimensions[0]]), spec.country_codes) ?? "", value: num(row[measure]) || 0,
    country: label(row[spec.dimensions[0]]) })).filter((item) => item.name);
  const top = Math.max(...values.map((v) => v.value), 0);
  return {
    tooltip: tooltip(palette, { trigger: "item", formatter: (p: { data?: { country: string; value: number }; name: string }) =>
      p.data ? tipHtml(p.data.country, [{ name: titleOf(data.annotation, measure), value: formatNumber(p.data.value, format) }], palette) : "" }),
    visualMap: { min: 0, max: top || 1, orient: "horizontal", left: "center", bottom: 0, itemHeight: 120, itemWidth: 10,
      inRange: { color: palette.sequential }, textStyle: { color: palette.muted, fontSize: 10 }, formatter: (v: number) => formatShort(v, format) },
    series: [{ type: "map", map: "nova-world", roam: false, top: 8, bottom: 36, data: values,
      itemStyle: { areaColor: palette.grid, borderColor: palette.surface, borderWidth: 0.5 },
      emphasis: { label: { show: false }, itemStyle: { areaColor: palette.series[0] } }, select: { disabled: true } }],
  };
}

// --- Numbers drawn as dials and bars -----------------------------------------------------------------

function gauge(spec: ChartSpec, data: ChartData, palette: Palette): Option {
  const measure = spec.measures[0];
  const format = formatOf(data.annotation, measure);
  const value = num(data.rows[0]?.[measure]);
  const max = spec.gauge_max ?? (format === "percent" ? 1 : Math.max(value * 1.25, 1));
  return {
    series: [{ type: "gauge", min: spec.gauge_min ?? 0, max, startAngle: 200, endAngle: -20, radius: "100%", center: ["50%", "62%"],
      progress: { show: true, width: 10, roundCap: true, itemStyle: { color: palette.series[0] } },
      axisLine: { lineStyle: { width: 10, color: [[1, palette.grid]] }, roundCap: true }, pointer: { show: false }, axisTick: { show: false },
      splitLine: { show: false }, axisLabel: { show: false }, anchor: { show: false }, title: { show: false },
      detail: { valueAnimation: true, offsetCenter: [0, "-6%"], fontSize: 24, fontWeight: 600, color: palette.text, formatter: (v: number) => formatShort(v, format) },
      data: [{ value }] }],
  };
}

function bullet(spec: ChartSpec, data: ChartData, palette: Palette): Option {
  const measure = spec.measures[0];
  const format = formatOf(data.annotation, measure);
  const value = num(data.rows[0]?.[measure]);
  const ranges = [...(spec.bullet_ranges ?? [])].sort((a, b) => a - b);
  const max = Math.max(...ranges, spec.target ?? 0, value) || 1;
  const shades = [0.55, 0.35, 0.18];
  return {
    grid: { left: 4, right: 12, top: 4, bottom: 20, containLabel: false },
    tooltip: tooltip(palette, { trigger: "axis", axisPointer: { type: "none" }, formatter: () => tipHtml(titleOf(data.annotation, measure), [
      { name: "Actual", value: formatNumber(value, format) }, ...(spec.target != null ? [{ name: "Target", value: formatNumber(spec.target, format) }] : [])], palette) }),
    xAxis: { type: "value", max, splitLine: { show: false }, axisLine: { show: false }, axisTick: { show: false },
      axisLabel: { color: palette.axis, fontSize: 10, formatter: (v: number) => formatShort(v, format) } },
    yAxis: { type: "category", data: [""], show: false },
    series: [
      ...ranges.map((edge, i) => ({ type: "bar", data: [edge - (ranges[i - 1] ?? 0)], stack: "bands", barWidth: "70%", silent: true,
        itemStyle: { color: palette.grid, opacity: shades[i] ?? 0.12 } })),
      { type: "bar", data: [value], barWidth: "28%", barGap: "-70%", z: 3, itemStyle: { color: palette.series[0], borderRadius: [0, 4, 4, 0] },
        markLine: spec.target == null ? undefined : { symbol: "none", silent: true, data: [{ xAxis: spec.target }],
          lineStyle: { color: palette.text, width: 2, type: "solid" }, label: { show: false } } },
    ],
  };
}

/** The options for a chart drawn by ECharts, or null for those drawn in HTML. */
export function optionFor(spec: ChartSpec, data: ChartData, palette: Palette): Option | null {
  switch (spec.viz) {
    case "line": case "smooth_line": case "step_line": case "area": case "bar": case "dots": case "combo":
      return cartesian(spec, data, palette);
    case "waterfall": return waterfall(spec, data, palette);
    case "pie": return pie(spec, data, palette);
    case "treemap": case "sunburst": return treemapOrSunburst(spec, data, palette);
    case "funnel": return funnel(spec, data, palette);
    case "histogram": return histogram(spec, data, palette);
    case "box_plot": return boxPlot(spec, data, palette);
    case "scatter": return scatter(spec, data, palette);
    case "heatmap": return heatmap(spec, data, palette);
    case "calendar": return calendar(spec, data, palette);
    case "radar": return radar(spec, data, palette);
    case "sankey": case "network": return flows(spec, data, palette);
    case "tree": return tree(spec, data, palette);
    case "world_map": return worldMap(spec, data, palette);
    case "gauge": return gauge(spec, data, palette);
    case "bullet": return bullet(spec, data, palette);
    default: return null;
  }
}

export const needsTime = (spec: ChartSpec) => timeOnX(spec);
