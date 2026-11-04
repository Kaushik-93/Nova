import type { RunEvent } from "@/lib/api";

export type Step = {
  id: number;
  label: string;
  detail?: string;
  text?: string;
  seconds?: number;
  tokens?: number;
  running: boolean;
  failed: boolean;
  startedAt: string;
};

export type Stage = { key: string; agent: string; steps: Step[]; seconds: number; running: boolean };

export const AGENT_NAMES: Record<string, string> = {
  main: "Nova",
  sql_author: "SQL author",
  chart_designer: "Chart designer",
  verifier: "Verifier",
  profiler: "Profiler",
};

const TOOL_LABELS: Record<string, string> = {
  list_sources: "Listing data sources",
  load_skill: "Reading the skill",
  check_skill_freshness: "Checking the skill is up to date",
  propose_skill_update: "Proposing a skill change",
  introspect_schema: "Looking at the schema",
  sample_table: "Sampling a table",
  run_readonly_sql: "Running a query",
  submit_plan: "Submitting the plan",
  ask_user: "Asking you a question",
  write_todos: "Updating its to-do list",
  write_file: "Saving a file",
  read_file: "Reading a file",
  edit_file: "Editing a file",
  ls: "Listing files",
  glob: "Searching files",
  grep: "Searching files",
  task: "Delegating",
  superset_build_status: "Checking what's built",
  superset_create_dataset: "Creating a dataset",
  superset_create_chart: "Creating a chart",
  superset_create_dashboard: "Creating the dashboard",
  superset_update_dashboard: "Updating the dashboard",
  superset_get_dashboard: "Reading the dashboard",
  superset_get_chart_data: "Checking a chart's data",
  render_dashboard_screenshot: "Opening it in a real browser",
  superset_guest_token: "Preparing the embed",
  finish_dashboard: "Running the final checks",
};

export const toolLabel = (tool: string) => TOOL_LABELS[tool] ?? tool;

/** Turns a stream of start and end events into steps, grouped by which agent was working. */
export function toStages(events: RunEvent[]): Stage[] {
  const stages: Stage[] = [];
  const open = new Map<string, Step[]>();

  const stageFor = (agent: string) => {
    const last = stages.at(-1);
    if (last?.agent === agent) return last;
    const stage: Stage = { key: `${agent}-${stages.length}`, agent, steps: [], seconds: 0, running: false };
    stages.push(stage);
    return stage;
  };
  const opened = (key: string, step: Step) => open.set(key, [...(open.get(key) ?? []), step]);
  const close = (key: string) => {
    const waiting = open.get(key);
    return waiting?.shift();
  };

  for (const event of events) {
    if (event.event === "model_start") {
      const step: Step = { id: event.id, label: "Thinking", running: true, failed: false, startedAt: event.at };
      stageFor(event.agent).steps.push(step);
      opened(`${event.agent}:model`, step);
    } else if (event.event === "model") {
      const step = close(`${event.agent}:model`);
      if (step) {
        step.running = false;
        step.seconds = event.seconds;
        step.tokens = event.tokens;
        step.text = event.text || undefined;
        if (event.tools?.length) {
          const names = [...new Set(event.tools.map(toolLabel))];
          step.label = `Decided: ${names.join(", ").toLowerCase()}${event.tools.length > names.length ? ` (${event.tools.length})` : ""}`;
        } else {
          step.label = "Reported back";
        }
      }
    } else if (event.event === "tool_start") {
      const step: Step = {
        id: event.id, label: toolLabel(event.tool ?? ""), detail: event.detail || undefined,
        running: true, failed: false, startedAt: event.at,
      };
      stageFor(event.agent).steps.push(step);
      opened(`${event.agent}:${event.tool}`, step);
    } else if (event.event === "tool_end") {
      const step = close(`${event.agent}:${event.tool}`);
      if (step) {
        step.running = false;
        step.seconds = event.seconds;
        step.failed = event.ok === false;
        if (event.result) step.text = event.result;
      }
    } else if (event.event === "tool_blocked") {
      stageFor(event.agent).steps.push({
        id: event.id, label: `Stopped: ${AGENT_NAMES[event.subagent ?? ""] ?? event.subagent} has reached its limit`,
        running: false, failed: true, startedAt: event.at,
      });
    }
  }
  for (const stage of stages) {
    stage.running = stage.steps.some((step) => step.running);
    stage.seconds = stage.steps.reduce((sum, step) => sum + (step.label === "Delegating" ? 0 : step.seconds ?? 0), 0);
  }
  return stages;
}

/** The step happening right now, for the status line. */
export function currentStep(stages: Stage[]): { agent: string; step: Step } | null {
  for (const stage of [...stages].reverse()) {
    const step = [...stage.steps].reverse().find((candidate) => candidate.running && candidate.label !== "Delegating");
    if (step) return { agent: stage.agent, step };
  }
  return null;
}
