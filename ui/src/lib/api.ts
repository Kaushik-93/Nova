import type { ChartData, DashboardSpec } from "@/lib/charts";

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:18000";

export type RunEvent = {
  id: number;
  at: string;
  agent: string;
  event: "model_start" | "model" | "tool_start" | "tool_end" | "tool_blocked" | "plan" | "chart_ready"
    | "dashboard_ready" | "dashboard_updated" | "connect_step" | "profile_table";
  step?: "catalog" | "profile" | "done";
  table?: string;
  done?: number;
  total?: number;
  plan?: Plan;
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
  measures: string[];
  dimensions: string[];
  grain: string | null;
  window?: string | null;
  width?: number;
};
export type Plan = { title: string; goal: string; sources: string[]; charts: PlannedChart[]; filters: string[]; assumptions: string[];
  new_definitions?: string[] };

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
export type DashboardRow = {
  slug: string;
  thread_id: string;
  title: string;
  summary: string;
  updated_at: string;
  created_by: string;
  sheet_name: string;
  project_slug: string;
  project_name: string;
};
export type ProjectSheet = { slug: string; thread_id: string; name: string; title: string; summary: string; updated_at: string };
export type Project = {
  slug: string;
  name: string;
  description: string;
  created_by: string;
  shared: boolean;
  updated_at: string;
  can_edit: boolean;
  sheets: ProjectSheet[];
};
export type DashboardDetail = {
  slug: string;
  thread_id: string;
  title: string;
  summary: string;
  updated_at: string;
  spec: DashboardSpec | null;
  needs_rebuild: boolean;
  prompt: string | null;
  can_edit: boolean;
  project: { slug: string; name: string } | null;
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
export type MemberStatus = "inferred" | "reviewed";
export type SemanticMember = {
  name: string; // cube.member
  short_name: string;
  title: string;
  description: string | null;
  type: string; // string, number, time, boolean; or a measure's count, sum, avg…
  format: string | null;
  unit: string | null;
  status: MemberStatus;
  reviewed_by: string | null;
  sql: string | null;
  filters: string[];
};
export type SemanticCube = {
  name: string;
  title: string;
  description: string | null;
  source: string; // its table, or the SELECT it reads
  measures: SemanticMember[];
  dimensions: SemanticMember[];
  segments: { name: string; title: string; description: string | null }[];
  joins: { cube: string; relationship: string }[];
};
export type SemanticSource = { source: string; display_name: string; catalog: string; cubes: SemanticCube[] };
export type QueryFilter = { member: string; operator: string; values: string[] };
export type SemanticQuery = {
  measures?: string[];
  dimensions?: string[];
  time_dimensions?: { dimension: string; granularity?: string | null; date_range?: string[] | null }[];
  filters?: QueryFilter[];
  segments?: string[];
  order?: { member: string; direction: "asc" | "desc" }[];
  limit?: number | null;
};
export type QueryResult = { columns: string[]; rows: Record<string, unknown>[]; row_count: number; certified: boolean;
  annotation: Record<string, Record<string, { title?: string; shortTitle?: string; type?: string; format?: string }>> };
export type Proposal = { index: number; cube: string; member: { name: string; title?: string; type: string; sql?: string; description?: string }; reason: string };
export type Skill = {
  name: string;
  description: string;
  version: number;
  status: string;
  generated_at: string;
  reviewed_by: string | null;
  guide: string;
  cubes: SemanticCube[];
  access: Record<string, { tenant_dimension?: string; shared?: boolean }>;
  proposals: Proposal[];
  example_queries: { question: string; query: SemanticQuery }[];
  gotchas: { kind: string; table: string; column: string; detail: string }[];
  tables: { name: string; rows: number | null }[];
  changelog: string;
};
export type Config = { model: string; provider: string };

let renderToken: string | null = null;
/** The renderer opens a dashboard with a short-lived token rather than a session. */
export function setRenderToken(token: string | null) {
  renderToken = token;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    credentials: "include", // the session is an HttpOnly cookie, which this page can't read
    headers: { "Content-Type": "application/json", ...(renderToken ? { Authorization: `Bearer ${renderToken}` } : {}), ...init?.headers },
    cache: "no-store",
  });
  if (response.status === 401 && !renderToken && path !== "/api/auth/login" && window.location.pathname !== "/login") {
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
  startRun: (prompt: string, threadId: string | null, requireApproval: boolean, project: string | null) =>
    request<{ run_id: string; thread_id: string }>("/api/runs", {
      method: "POST",
      body: JSON.stringify({ prompt, thread_id: threadId, require_approval: requireApproval, project }),
    }),
  resumeRun: (runId: string, body: { plan?: Plan; rejected?: boolean; reason?: string; answer?: string }) =>
    request<{ run_id: string; thread_id: string }>(`/api/runs/${runId}/resume`, { method: "POST", body: JSON.stringify(body) }),
  getRun: (runId: string) => request<Run>(`/api/runs/${runId}`),
  listRuns: (threadId?: string) => request<RunListItem[]>(`/api/runs${threadId ? `?thread_id=${threadId}` : ""}`),
  listDashboards: () => request<DashboardRow[]>("/api/dashboards"),
  projects: () => request<Project[]>("/api/projects"),
  project: (slug: string) => request<Project>(`/api/projects/${slug}`),
  createProject: (name: string, description: string) =>
    request<Project>("/api/projects", { method: "POST", body: JSON.stringify({ name, description }) }),
  changeProject: (slug: string, body: { name?: string; description?: string; shared?: boolean }) =>
    request<Project>(`/api/projects/${slug}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteProject: (slug: string) => request(`/api/projects/${slug}`, { method: "DELETE" }),
  reorderSheets: (slug: string, sheets: string[]) =>
    request<Project>(`/api/projects/${slug}/order`, { method: "PUT", body: JSON.stringify({ sheets }) }),
  changeSheet: (slug: string, sheet: string, name: string) =>
    request<Project>(`/api/projects/${slug}/sheets/${sheet}`, { method: "PATCH", body: JSON.stringify({ name }) }),
  deleteSheet: (slug: string, sheet: string) => request(`/api/projects/${slug}/sheets/${sheet}`, { method: "DELETE" }),
  dashboard: (slug: string) => request<DashboardDetail>(`/api/dashboards/${slug}`),
  chartData: (slug: string, key: string, body: { filters: Record<string, string[]>; date_range: string[] | null }) =>
    request<ChartData>(`/api/dashboards/${slug}/charts/${key}/data`, { method: "POST", body: JSON.stringify(body) }),
  filterOptions: (slug: string, name: string) => request<string[]>(`/api/dashboards/${slug}/filters/${encodeURIComponent(name)}/options`),
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
  approveMember: (slug: string, member: string) => {
    const [cube, name] = member.split(".");
    return request(`/api/skills/${slug}/members/${cube}/${name}/approve`, { method: "POST", body: "{}" });
  },
  decideProposal: (slug: string, index: number, accept: boolean) =>
    request(`/api/skills/${slug}/proposals/${index}`, { method: "POST", body: JSON.stringify({ accept }) }),
  semanticMeta: () => request<SemanticSource[]>("/api/semantic/meta"),
  semanticQuery: (query: SemanticQuery) => request<QueryResult>("/api/semantic/query", { method: "POST", body: JSON.stringify({ query }) }),
  semanticSql: (query: SemanticQuery) => request<{ sql: string }>("/api/semantic/sql", { method: "POST", body: JSON.stringify({ query }) }),
};

/** A query's results as a CSV download. Fetched rather than linked, because it's a POST with the session. */
export async function downloadCsv(query: SemanticQuery, filename = "nova-query.csv") {
  const response = await fetch(`${API_URL}/api/semantic/csv`, {
    method: "POST", credentials: "include", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ query }),
  });
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error ?? `The API returned ${response.status}`);
  const url = URL.createObjectURL(await response.blob());
  const link = Object.assign(document.createElement("a"), { href: url, download: filename });
  link.click();
  URL.revokeObjectURL(url);
}
