/** Charts as Nova stores them (agent/semantic/charts.py), and reading their data from Cube's rows. */

export type Viz =
  | "big_number" | "big_number_trend" | "gauge" | "bullet"
  | "line" | "smooth_line" | "step_line" | "area" | "bar" | "dots" | "combo" | "waterfall"
  | "pie" | "treemap" | "sunburst" | "funnel"
  | "histogram" | "box_plot" | "scatter" | "heatmap" | "calendar" | "radar"
  | "sankey" | "network" | "tree" | "world_map" | "table" | "pivot" | "word_cloud";

export type ChartSpec = {
  key: string;
  title: string;
  viz: Viz;
  subtitle?: string | null;
  measures: string[];
  dimensions: string[];
  time_dimension?: string | null;
  grain?: "day" | "week" | "month" | "quarter" | "year" | null;
  periods?: number | null;
  include_partial_period?: boolean;
  time_axis?: boolean | null;
  limit?: number;
  horizontal?: boolean;
  stacking?: "none" | "stacked" | "percent";
  donut?: boolean;
  line_measures?: string[];
  pivot_columns?: string[];
  totals?: boolean;
  distribute_across?: string[];
  value_dimension?: string | null;
  bins?: number;
  gauge_min?: number;
  gauge_max?: number | null;
  target?: number | null;
  bullet_ranges?: number[];
  country_codes?: "cca2" | "cca3" | "name";
  tree_root?: string | null;
};

export type DashboardFilter = { name: string; member: string; applies_to: string[] };
export type DashboardSpec = {
  title: string;
  charts: ChartSpec[];
  rows: { key: string; width: number }[][];
  filters: DashboardFilter[];
  time_filter: boolean;
};

/** What Cube says about a member, plus `additive` from Nova: whether its values can be summed into a total. */
type MemberNote = { title?: string; shortTitle?: string; type?: string; format?: string; meta?: { unit?: string }; additive?: boolean };
export type Annotation = { measures?: Record<string, MemberNote>; dimensions?: Record<string, MemberNote>; timeDimensions?: Record<string, MemberNote> };
export type ChartData = {
  key: string;
  rows: Record<string, unknown>[];
  row_count: number;
  truncated: boolean;
  window: [string, string] | null;
  annotation: Annotation;
  certified: boolean;
};

export const KPIS = new Set<Viz>(["big_number", "big_number_trend", "gauge", "bullet"]);
const TIME_SERIES = new Set<Viz>(["line", "smooth_line", "step_line", "area", "big_number_trend"]);
const X_AXIS = new Set<Viz>([...TIME_SERIES, "bar", "dots", "combo", "waterfall", "heatmap"]);

/** Whether time runs along the bottom; the same rule as the server's. */
export function timeOnX(spec: ChartSpec): boolean {
  if (TIME_SERIES.has(spec.viz) || spec.viz === "calendar") return true;
  if (!X_AXIS.has(spec.viz) || !spec.time_dimension || !spec.grain) return false;
  if (spec.time_axis != null) return spec.time_axis;
  return spec.dimensions.length < (spec.viz === "heatmap" ? 2 : 1);
}

/** The key a time dimension's values have in a row. */
export function timeKey(spec: ChartSpec): string | null {
  if (!spec.time_dimension || !timeOnX(spec)) return null;
  return `${spec.time_dimension}.${spec.viz === "calendar" ? "day" : spec.grain}`;
}

export const num = (value: unknown) => (value == null || value === "" ? NaN : Number(value));

export function titleOf(annotation: Annotation, member: string): string {
  const note = annotation.measures?.[member] ?? annotation.dimensions?.[member] ?? annotation.timeDimensions?.[member];
  return note?.shortTitle ?? note?.title ?? member.split(".").at(-1)!.replaceAll("_", " ");
}

export type Format = "currency" | "percent" | "number";
export function formatOf(annotation: Annotation, member: string | undefined): Format {
  const note = member ? annotation.measures?.[member] : undefined;
  if (note?.format === "currency" || note?.meta?.unit === "USD") return "currency";
  if (note?.format === "percent") return "percent";
  return "number";
}

const COMPACT = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });
const WHOLE = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const PRECISE = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });

/** A value in full, for a number, table or tooltip. */
export function formatNumber(value: unknown, format: Format = "number"): string {
  const n = num(value);
  if (Number.isNaN(n)) return value == null ? "–" : String(value);
  if (format === "percent") return `${(n * 100).toFixed(1)}%`;
  const text = (Math.abs(n) >= 100 ? WHOLE : PRECISE).format(n);
  return format === "currency" ? (n < 0 ? `-$${text.slice(1)}` : `$${text}`) : text;
}

/** A value on an axis, or a big number: in full under 10,000 and compact above. */
export function formatShort(value: unknown, format: Format = "number"): string {
  const n = num(value);
  if (Number.isNaN(n)) return "–";
  if (format === "percent") return `${(n * 100).toFixed(Math.abs(n) < 0.1 ? 1 : 0)}%`;
  const text = Math.abs(n) >= 10_000 ? COMPACT.format(n) : (Math.abs(n) >= 100 ? WHOLE : PRECISE).format(n);
  return format === "currency" ? `$${text}` : text;
}

const LABELS = {
  day: new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", timeZone: "UTC" }),
  week: new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", timeZone: "UTC" }),
  month: new Intl.DateTimeFormat("en-US", { month: "short", year: "numeric", timeZone: "UTC" }),
};

export function formatPeriod(value: unknown, grain?: string | null): string {
  const date = new Date(`${String(value).slice(0, 19)}Z`);
  if (Number.isNaN(date.getTime())) return String(value ?? "");
  if (grain === "year") return String(date.getUTCFullYear());
  if (grain === "quarter") return `Q${Math.floor(date.getUTCMonth() / 3) + 1} ${date.getUTCFullYear()}`;
  return (LABELS[grain as keyof typeof LABELS] ?? LABELS.day).format(date);
}

const RANGE = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
export function formatWindow(window: [string, string] | null): string | null {
  if (!window) return null;
  const [from, to] = window;
  if (from.startsWith("1900")) return `Up to ${RANGE.format(new Date(`${to}T00:00:00Z`))}`;
  return `${RANGE.format(new Date(`${from}T00:00:00Z`))} – ${RANGE.format(new Date(`${to}T00:00:00Z`))}`;
}

export function label(value: unknown): string {
  return value == null || value === "" ? "(none)" : String(value);
}

/** Row widths in columns of 12: given widths are kept and the rest share what's left. */
export function widths(row: { width: number }[]): number[] {
  const fixed = row.reduce((sum, slot) => sum + slot.width, 0);
  const flexible = row.filter((slot) => !slot.width).length;
  if (!flexible) return row.map((slot) => slot.width);
  const room = Math.max(12 - fixed, flexible);
  const share = Math.floor(room / flexible);
  let extra = room - share * flexible;
  return row.map((slot) => slot.width || share + (extra-- > 0 ? 1 : 0));
}

/** A layout for charts that haven't been laid out yet: numbers first, then two charts a row. */
export function defaultRows(charts: ChartSpec[]): { key: string; width: number }[][] {
  const kpis = charts.filter((chart) => KPIS.has(chart.viz));
  const rest = charts.filter((chart) => !KPIS.has(chart.viz));
  const rows: { key: string; width: number }[][] = [];
  for (let i = 0; i < kpis.length; i += 4) rows.push(kpis.slice(i, i + 4).map((c) => ({ key: c.key, width: 0 })));
  for (let i = 0; i < rest.length; i += 2) rows.push(rest.slice(i, i + 2).map((c) => ({ key: c.key, width: 0 })));
  return rows;
}
