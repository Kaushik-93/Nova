export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:18000";

export type RunEvent = {
  id: number;
  at: string;
  agent: string;
  event: "model_start" | "model" | "tool_start" | "tool_end" | "tool_blocked" | "plan" | "chart_ready"
    | "dashboard_ready" | "dashboard_updated";
  plan?: Plan;
  chart_id?: number;
  key?: string;
  title?: string;
  viz?: string;
  slug?: string;
  tool?: string;
  subagent?: string;
  detail?: string;
  text?: string;
  result?: string;
  seconds?: number;
  tokens?: number;
  tools?: string[];
  ok?: boolean;
};

export type Timing = {
  wall_seconds: number | null;
  model_seconds: number;
  tool_seconds: number;
  turns: number;
  tokens: number;
  repair_rounds: number;
  delegations: Record<string, number>;
  agents: Record<string, { turns: number; tokens: number; model_seconds: number; tool_seconds: number }>;
  slowest: { agent: string; label: string; seconds: number }[];
  failures: string[];
  explanation: string[];
};

export type PlannedChart = {
  key: string;
  title: string;
  viz: string;
  metric: string;
  dimensions: string[];
  time_grain: string | null;
  width?: number;
};
export type Plan = { title: string; goal: string; sources: string[]; charts: PlannedChart[]; filters: string[]; assumptions: string[] };

export type RunResult = {
  status: string;
  dashboard_slug?: string | null;
  title?: string | null;
  summary?: string;
  definitions?: string[];
  warnings?: string[];
  plan?: Plan | null;
  question?: string | null;
  error?: string | null;
};

export type Run = {
  run_id: string;
  thread_id: string;
  prompt: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  wall_seconds: number;
  result: RunResult;
  timing: Timing;
};

export type RunListItem = {
  run_id: string;
  thread_id: string;
  prompt: string;
  status: string;
  started_at: string;
  title: string | null;
  summary: string | null;
};
export type ChartPreview = {
  chart_id: number;
  title: string;
  viz: string;
  columns: string[];
  rows: Record<string, unknown>[];
  row_count: number;
  x_axis: string | null;
  groupby: string[];
  time_grain: string | null;
  format: string | null;
};
export type DashboardRow = { slug: string; thread_id: string; superset_id: number; title: string; summary: string; updated_at: string };
export type EmbedInfo = {
  slug: string;
  title: string;
  embed_uuid: string;
  guest_token: string;
  superset_url: string;
  filters: { id: string; name: string; column: string | null }[];
};
export type SourceRow = {
  slug: string;
  display_name: string;
  trino_catalog: string;
  dialect: string;
  skill?: { name: string; version: number; status: string };
};
export type Metric = { label: string; sql: string; base: string; filters: string[]; unit: string | null; description: string | null; status: string };
export type Skill = {
  name: string;
  description: string;
  version: number;
  status: string;
  generated_at: string;
  reviewed_by: string | null;
  guide: string;
  metrics: Record<string, Metric>;
  proposed: Record<string, Metric>;
  gotchas: { kind: string; table: string; column: string; detail: string }[];
  tables: { name: string; rows: number | null }[];
  examples: string;
  changelog: string;
};
export type Config = { superset_url: string; model: string; provider: string };

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
    cache: "no-store",
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error ?? `The API returned ${response.status}`);
  }
  return response.json();
}

export const api = {
  config: () => request<Config>("/api/config"),
  startRun: (prompt: string, threadId: string | null, requireApproval: boolean) =>
    request<{ run_id: string; thread_id: string }>("/api/runs", {
      method: "POST",
      body: JSON.stringify({ prompt, thread_id: threadId, require_approval: requireApproval }),
    }),
  resumeRun: (runId: string, body: { plan?: Plan; rejected?: boolean; reason?: string; answer?: string }) =>
    request<{ run_id: string; thread_id: string }>(`/api/runs/${runId}/resume`, { method: "POST", body: JSON.stringify(body) }),
  getRun: (runId: string) => request<Run>(`/api/runs/${runId}`),
  listRuns: (threadId?: string) => request<RunListItem[]>(`/api/runs${threadId ? `?thread_id=${threadId}` : ""}`),
  listDashboards: () => request<DashboardRow[]>("/api/dashboards"),
  embed: (slug: string) => request<EmbedInfo>(`/api/dashboards/${slug}/embed`),
  chartPreview: (chartId: number) => request<ChartPreview>(`/api/charts/${chartId}/preview`),
  sources: () => request<SourceRow[]>("/api/sources"),
  skill: (slug: string) => request<Skill>(`/api/skills/${slug}`),
  approveSkill: (slug: string) => request(`/api/skills/${slug}/approve`, { method: "POST", body: "{}" }),
};
