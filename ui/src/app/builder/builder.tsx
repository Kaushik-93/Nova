"use client";

import { ExternalLink, Filter, Loader2, Maximize2, Minimize2, Send, SquarePen } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { DashboardEmbed, type FilterSelection } from "@/components/dashboard-embed";
import { PlanCard } from "@/components/plan-card";
import { RunTimeline } from "@/components/run-timeline";
import { EmptyState, ErrorState } from "@/components/states";
import { TimingPanel } from "@/components/timing-panel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { api, type Config, type Plan } from "@/lib/api";
import { AGENT_NAMES, currentStep, toStages } from "@/lib/steps";
import { useNow, useRun } from "@/lib/use-run";
import { cn, formatSeconds } from "@/lib/utils";

const SUGGESTION = "Create a rental performance dashboard for the last 12 months covering revenue, rental agreements, the vehicle fleet and vehicle damages. I want to filter the whole dashboard by tenant name.";

function describeSelection(selection: FilterSelection) {
  return selection.map((filter) => `${filter.name}: ${filter.values.join(", ")}`).join("; ");
}

export function Builder() {
  const router = useRouter();
  const params = useSearchParams();
  const threadFromUrl = params.get("thread");

  const [threadId, setThreadId] = useState<string | null>(threadFromUrl);
  const [runId, setRunId] = useState<string | null>(null);
  const [prompt, setPrompt] = useState("");
  const [requireApproval, setRequireApproval] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selection, setSelection] = useState<FilterSelection>([]);
  const [config, setConfig] = useState<Config | null>(null);
  const { events, run, lost } = useRun(runId);

  const stages = useMemo(() => toStages(events), [events]);
  const status = run?.status;
  const running = !run || status === "running" ? Boolean(runId) : false;
  const now = useNow(running);
  const current = currentStep(stages);
  const result = run?.result;
  const elapsed = run ? (running ? (now - new Date(run.started_at).getTime()) / 1000 : run.wall_seconds) : 0;
  const [slug, setSlug] = useState<string | null>(null);
  const [wide, setWide] = useState(false);
  // The plan arrives as an event, so it shows while the agent is still building.
  const livePlan = useMemo(() => events.findLast((event) => event.event === "plan")?.plan, [events]);
  const plan = result?.plan ?? livePlan;

  useEffect(() => { api.config().then(setConfig).catch(() => undefined); }, []);

  // Coming back to a conversation shows its latest run and dashboard.
  useEffect(() => {
    if (!threadFromUrl) return;
    setThreadId(threadFromUrl);
    api.listRuns(threadFromUrl).then((runs) => { if (runs[0]) setRunId(runs[0].run_id); }).catch(() => undefined);
  }, [threadFromUrl]);

  useEffect(() => { if (result?.dashboard_slug) setSlug(result.dashboard_slug); }, [result?.dashboard_slug]);

  async function send(text: string) {
    setError(null);
    try {
      // The agent can't see inside the dashboard, so it's told what the person is looking at.
      const context = selection.length ? `\n\n(The dashboard is currently filtered to ${describeSelection(selection)}.)` : "";
      const started = await api.startRun(text + context, threadId, requireApproval);
      setThreadId(started.thread_id);
      setRunId(started.run_id);
      setPrompt("");
      router.replace(`/builder?thread=${started.thread_id}`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The run couldn't be started.");
    }
  }

  async function resume(body: { plan?: Plan; rejected?: boolean; reason?: string; answer?: string }) {
    if (!runId) return;
    setError(null);
    try {
      setRunId((await api.resumeRun(runId, body)).run_id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The run couldn't be resumed.");
    }
  }

  function startOver() {
    setThreadId(null); setRunId(null); setSlug(null); setSelection([]); setError(null);
    router.replace("/builder");
  }

  return (
    <div className={cn("grid h-full gap-3 p-3", wide ? "grid-cols-1" : "grid-cols-[340px_300px_minmax(0,1fr)]")}>
      <section className={cn("flex min-h-0 flex-col gap-3", wide && "hidden")} aria-label="Conversation">
        <Card className="shrink-0">
          <CardContent className="space-y-2 p-3">
            <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} rows={4} aria-label="What do you want to build?"
              placeholder={threadId ? "Ask for a change to this dashboard…" : "Describe the dashboard you want…"}
              className="w-full resize-none rounded-md border border-border bg-background p-2 text-sm"
              onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && prompt.trim() && !running) send(prompt); }} />
            {selection.length > 0 && (
              <p className="flex items-center gap-1 text-[11px] text-muted-foreground">
                <Filter className="size-3" /> The agent will know you&apos;re viewing {describeSelection(selection)}
              </p>
            )}
            <div className="flex items-center justify-between gap-2">
              <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <input type="checkbox" checked={requireApproval} onChange={(e) => setRequireApproval(e.target.checked)} />
                Approve the plan first
              </label>
              <div className="flex gap-1">
                {threadId && <Button size="icon" variant="ghost" aria-label="New dashboard" onClick={startOver}><SquarePen className="size-4" /></Button>}
                <Button size="sm" disabled={!prompt.trim() || running} onClick={() => send(prompt)}><Send className="size-3" /> Send</Button>
              </div>
            </div>
            {!threadId && !prompt && (
              <button className="text-left text-[11px] text-primary hover:underline" onClick={() => setPrompt(SUGGESTION)}>
                Try the rental dashboard
              </button>
            )}
            {config && <p className="text-[11px] text-muted-foreground">Model: {config.model}</p>}
          </CardContent>
        </Card>

        {error && <ErrorState message={error} />}
        {running && (
          <div className="flex shrink-0 items-center gap-2 rounded-md bg-accent px-3 py-2 text-xs text-primary" role="status">
            <Loader2 className="size-3 shrink-0 animate-spin" />
            <span className="min-w-0 flex-1 truncate">
              {current ? `${AGENT_NAMES[current.agent] ?? current.agent}: ${current.step.label.toLowerCase()}` : "Starting…"}
            </span>
            <span className="tabular-nums">{formatSeconds(elapsed)}</span>
          </div>
        )}
        {lost && running && <ErrorState message="Lost the connection to the API. Reconnecting…" />}

        <div className="min-h-0 flex-1 overflow-auto">
          {stages.length > 0 ? <RunTimeline stages={stages} active={running} />
            : !runId && <EmptyState title="Nothing running yet">The agent&apos;s steps appear here as it works.</EmptyState>}
        </div>
      </section>

      <section className={cn("flex min-h-0 flex-col gap-3 overflow-auto", wide && "hidden")} aria-label="Plan and timing">
        {plan && (
          <PlanCard plan={plan} editable={status === "awaiting_approval"}
            onApprove={(plan) => resume({ plan })} onReject={(reason) => resume({ rejected: true, reason })} />
        )}
        {status === "awaiting_answer" && result?.question && (
          <Card>
            <CardContent className="space-y-2 p-3">
              <p className="text-sm">{result.question}</p>
              <textarea aria-label="Your answer" rows={2} className="w-full rounded-md border border-border bg-background p-2 text-sm"
                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); resume({ answer: e.currentTarget.value }); } }} />
            </CardContent>
          </Card>
        )}
        {run?.timing && (run.timing.turns > 0) && <TimingPanel timing={run.timing} wallSeconds={elapsed} />}
        {result?.error && <ErrorState message={result.error} />}
        {status === "partial" && <Badge variant="warning">Stopped by its budget. What was finished is kept.</Badge>}
        {result?.summary && (
          <Card>
            <CardContent className="space-y-2 p-3 text-xs leading-relaxed">
              <p>{result.summary}</p>
              {(result.definitions?.length ?? 0) > 0 && (
                <details>
                  <summary className="cursor-pointer text-primary">Definitions used</summary>
                  <ul className="mt-1 list-disc space-y-1 pl-4 text-muted-foreground">
                    {result.definitions?.map((definition) => <li key={definition}>{definition}</li>)}
                  </ul>
                </details>
              )}
              {result.warnings?.map((warning) => <p key={warning} className="text-warning">{warning}</p>)}
            </CardContent>
          </Card>
        )}
      </section>

      <section className="flex min-h-0 flex-col gap-2" aria-label="Dashboard">
        {slug && (
          <div className="flex shrink-0 items-center gap-2">
            <h1 className="min-w-0 truncate text-sm font-semibold">{result?.title}</h1>
            {selection.map((filter) => (
              <Badge key={filter.name} variant="primary" data-testid="selected-filter"><Filter className="size-3" /> {filter.name}: {filter.values.join(", ")}</Badge>
            ))}
            <Button size="sm" variant="ghost" className="ml-auto shrink-0" onClick={() => setWide(!wide)}>
              {wide ? <Minimize2 className="size-3" /> : <Maximize2 className="size-3" />}
              {wide ? "Show the agent" : "Full width"}
            </Button>
            {config && (
              <a className="flex shrink-0 items-center gap-1 text-xs text-primary hover:underline"
                href={`${config.superset_url}/superset/dashboard/${slug}/`} target="_blank" rel="noreferrer">
                Open in Superset <ExternalLink className="size-3" />
              </a>
            )}
          </div>
        )}
        <div className="min-h-0 flex-1 overflow-hidden rounded-lg border border-border bg-card">
          {slug && !running ? <DashboardEmbed key={slug} slug={slug} onSelection={setSelection} />
            : <div className="flex h-full items-center justify-center p-8">
                <EmptyState title={running ? "Building your dashboard" : "Your dashboard appears here"}>
                  {running ? "It opens here when the checks pass." : "Describe what you want on the left."}
                </EmptyState>
              </div>}
        </div>
      </section>
    </div>
  );
}
