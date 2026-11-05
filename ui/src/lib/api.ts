export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:18000";

export type RunEvent = {
  id: number;
  at: string;
  agent: string;
  event: "model_start" | "model" | "tool_start" | "tool_end" | "tool_blocked" | "plan" | "chart_ready"
    | "dashboard_ready" | "dashboard_updated" | "connect_step" | "profile_table";
  step?: "superset" | "catalog" | "profile" | "done";
  table?: string;
  done?: number;
  total?: number;
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
export type DashboardRow = {
  slug: string;
  thread_id: string;
  superset_id: number;
  title: string;
  summary: string;
  updated_at: string;
  created_by: string;
  shared: boolean;
};
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
  dialect: string;
  status: string;
  // Only administrators get these.
  trino_catalog?: string;
  read_only?: boolean | null;
  tenant_column?: string | null;
  tenant_table?: string | null;
  skill?: { name: string; version: number; status: string };
};
export type Role = "admin" | "analyst" | "viewer";
export type User = { id: string; email: string; display_name: string; role: Role; is_active: boolean; uses_sso: boolean };
export type AccessEntry = { source: string; tenant_ids: string[] | null };
export type ManagedUser = User & { access: AccessEntry[] };
export type AuthOptions = { sso: { enabled: boolean; name: string } };
export type DemoSource = { slug: string; name: string; description: string; connected: boolean };
export type Connection = { name: string; kind: "postgres" | "mysql"; host: string; port: number; database: string; username: string; password: string };
export type ConnectionTest = { connected: boolean; read_only: boolean; warning: string | null };
export type Started = { run_id: string; thread_id: string; slug: string; read_only: boolean; warning: string | null };
export type TenancySuggestion = { column: string; table: string | null; label_column: string | null; tables_without_column: string[] };
export type Tenancy = {
  column: string | null;
  table: string | null;
  label_column: string | null;
  shared_tables: string[];
  suggestion: TenancySuggestion | null;
  tables: string[];
};
export type TenantOption = { id: string; name: string };
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
export type Config = { superset_url: string | null; model: string; provider: string };

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    credentials: "include", // the session is an HttpOnly cookie, which this page can't read
    headers: { "Content-Type": "application/json", ...init?.headers },
    cache: "no-store",
  });
  if (response.status === 401 && path !== "/api/auth/login" && window.location.pathname !== "/login") {
    window.location.assign("/login");
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error ?? `The API returned ${response.status}`);
  }
  return response.json();
}

export const api = {
  authOptions: () => request<AuthOptions>("/api/auth/options"),
  login: (email: string, password: string) => request<User>("/api/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }),
  logout: () => request("/api/auth/logout", { method: "POST" }),
  me: () => request<User>("/api/auth/me"),
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
  shareDashboard: (slug: string, shared: boolean) =>
    request(`/api/dashboards/${slug}`, { method: "PATCH", body: JSON.stringify({ shared }) }),
  embed: (slug: string) => request<EmbedInfo>(`/api/dashboards/${slug}/embed`),
  chartPreview: (chartId: number) => request<ChartPreview>(`/api/charts/${chartId}/preview`),
  sources: () => request<SourceRow[]>("/api/sources"),
  demoSources: () => request<DemoSource[]>("/api/sources/demos"),
  connectDemo: (slug: string) => request<Started>(`/api/sources/demos/${slug}`, { method: "POST" }),
  testSource: (connection: Connection) => request<ConnectionTest>("/api/sources/test", { method: "POST", body: JSON.stringify(connection) }),
  connectSource: (connection: Connection) => request<Started>("/api/sources", { method: "POST", body: JSON.stringify(connection) }),
  profileSource: (slug: string) => request<{ run_id: string }>(`/api/sources/${slug}/profile`, { method: "POST" }),
  tenancy: (slug: string) => request<Tenancy>(`/api/sources/${slug}/tenancy`),
  setTenancy: (slug: string, body: { column: string | null; table: string | null; label_column: string | null; shared_tables: string[] }) =>
    request(`/api/sources/${slug}/tenancy`, { method: "PUT", body: JSON.stringify(body) }),
  tenants: (slug: string) => request<TenantOption[]>(`/api/sources/${slug}/tenants`),
  users: () => request<ManagedUser[]>("/api/users"),
  createUser: (body: { email: string; display_name: string; password: string | null; role: Role }) =>
    request<User>("/api/users", { method: "POST", body: JSON.stringify(body) }),
  changeUser: (id: string, body: { role?: Role; is_active?: boolean; password?: string }) =>
    request<User>(`/api/users/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  setAccess: (id: string, entries: AccessEntry[]) =>
    request<AccessEntry[]>(`/api/users/${id}/access`, { method: "PUT", body: JSON.stringify(entries) }),
  skill: (slug: string) => request<Skill>(`/api/skills/${slug}`),
  approveSkill: (slug: string) => request(`/api/skills/${slug}/approve`, { method: "POST", body: "{}" }),
};
