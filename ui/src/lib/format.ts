const COMPACT = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });
const GROUPED = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const PRECISE = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });

const isMoney = (format?: string | null) => Boolean(format?.startsWith("$"));
const isPercent = (format?: string | null) => Boolean(format?.includes("%"));

/** A value in full, for a KPI, table or tooltip. Understands the d3 formats charts carry. */
export function formatValue(value: unknown, format?: string | null): string {
  if (typeof value !== "number") return value == null ? "–" : String(value);
  if (isPercent(format)) return `${(value * 100).toFixed(1)}%`;
  const text = (Math.abs(value) >= 100 ? GROUPED : PRECISE).format(value);
  return isMoney(format) ? `$${text}` : text;
}

/** A value on an axis: in full under 10,000 and compact above. */
export function formatAxis(value: number, format?: string | null): string {
  if (isPercent(format)) return `${(value * 100).toFixed(0)}%`;
  const text = Math.abs(value) >= 10_000 ? COMPACT.format(value) : GROUPED.format(value);
  return isMoney(format) ? `$${text}` : text;
}

const MONTH = new Intl.DateTimeFormat("en-US", { month: "short", year: "numeric", timeZone: "UTC" });
const DAY = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", timeZone: "UTC" });

export function formatTime(value: number, grain?: string | null): string {
  const date = new Date(value);
  if (grain === "P1Y") return String(date.getUTCFullYear());
  if (grain === "P3M") return `Q${Math.floor(date.getUTCMonth() / 3) + 1} ${date.getUTCFullYear()}`;
  if (grain === "P1M") return MONTH.format(date);
  return DAY.format(date);
}

export function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}
