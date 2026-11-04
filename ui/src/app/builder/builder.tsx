"use client";

import { ArrowUp, CheckCircle2, ExternalLink, Filter, Loader2, Maximize2, Minimize2, Plus, ShieldAlert, TriangleAlert } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Welcome } from "@/app/builder/welcome";
import { DashboardEmbed, type FilterSelection } from "@/components/dashboard-embed";
import { CanvasPlaceholder, LiveCanvas } from "@/components/live-canvas";
import { PlanCard } from "@/components/plan-card";
import { ProgressSteps } from "@/components/progress-steps";
import { RunTimeline } from "@/components/run-timeline";
import { EmptyState, ErrorState, Loading } from "@/components/states";
import { TimingPanel } from "@/components/timing-panel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { api, type Config, type Plan, type RunListItem } from "@/lib/api";
import { toMilestones, toStages } from "@/lib/steps";
import { useNow, useRun } from "@/lib/use-run";
import { cn } from "@/lib/utils";

type Tab = "progress" | "plan" | "timing";
const CONTEXT_MARK = "\n\n(The dashboard is currently filtered to";

function describeSelection(selection: FilterSelection) {
  return selection.map((filter) => `${filter.name}: ${filter.values.join(", ")}`).join("; ");
}

function Status({ status, hasDashboard, warnings }: { status?: string; hasDashboard: boolean; warnings: number }) {
  if (status === "running" && hasDashboard) return <Badge variant="primary"><Loader2 className="size-3 animate-spin" /> Checks running</Badge>;
  if (status === "running") return <Badge variant="primary"><Loader2 className="size-3 animate-spin" /> Building</Badge>;
  if (status === "awaiting_approval") return <Badge variant="warning">Waiting for your approval</Badge>;
  if (status === "awaiting_answer") return <Badge variant="warning">Waiting for your answer</Badge>;
  if (status === "partial") return <Badge variant="warning"><TriangleAlert className="size-3" /> Stopped by its budget</Badge>;
  if (status === "failed" || status === "interrupted") return <Badge variant="destructive"><ShieldAlert className="size-3" /> Not finished</Badge>;
  if (status === "finished" && warnings) return <Badge variant="warning"><TriangleAlert className="size-3" /> Finished with warnings</Badge>;
  if (status === "finished") return <Badge variant="success"><CheckCircle2 className="size-3" /> Verified</Badge>;
  return null;
}

export function Builder() {
  const router = useRouter();
  const threadFromUrl = useSearchParams().get("thread");

  const [threadId, setThreadId] = useState<string | null>(threadFromUrl);
  const [runId, setRunId] = useState<string | null>(null);
  const [history, setHistory] = useState<RunListItem[]>([]);
  const [prompt, setPrompt] = useState("");
  const [requireApproval, setRequireApproval] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selection, setSelection] = useState<FilterSelection>([]);
  const [config, setConfig] = useState<Config | null>(null);
  const [wide, setWide] = useState(false);
  const [tab, setTab] = useState<Tab>("progress");
  const [loadedEmbed, setLoadedEmbed] = useState<string | null>(null);
  const answer = useRef<HTMLTextAreaElement>(null);
  const { events, run, lost } = useRun(runId);

  const status = run?.status;
  const running = Boolean(runId) && (!run || status === "running");
  const now = useNow(running);
  const stages = useMemo(() => toStages(events), [events]);
  const milestones = useMemo(() => toMilestones(events, status, now), [events, status, now]);
  const result = run?.result;
  const elapsed = run ? (running ? (now - new Date(run.started_at).getTime()) / 1000 : run.wall_seconds) : 0;

  // The plan and dashboard arrive as events, so they appear while the agent is still working.
  const livePlan = useMemo(() => events.findLast((event) => event.event === "plan")?.plan, [events]);
  const plan = result?.plan ?? livePlan;
  const liveSlug = useMemo(() => events.find((event) => event.event === "dashboard_ready")?.slug, [events]);
  const [knownSlug, setKnownSlug] = useState<string | null>(null);
  const slug = result?.dashboard_slug ?? liveSlug ?? knownSlug;
  const title = result?.title ?? events.find((event) => event.event === "dashboard_ready")?.title ?? plan?.title;

  // Charts changed after the embed opened, so show the final version once the run ends.
  const firstDashboard = events.findIndex((event) => event.event === "dashboard_ready");
  const revisions = firstDashboard < 0 ? 0 : events.slice(firstDashboard + 1).filter((event) => event.event === "chart_ready" || event.event === "dashboard_updated").length;
  const embedKey = `${slug}:${running ? 0 : revisions}`;
  const embedLoaded = loadedEmbed === embedKey;
  const markLoaded = useCallback(() => setLoadedEmbed(embedKey), [embedKey]);

  useEffect(() => { api.config().then(setConfig).catch(() => undefined); }, []);

  const loadHistory = useCallback((thread: string) => {
    api.listRuns(thread).then((runs) => {
      setHistory([...runs].reverse());
      setRunId((current) => current ?? runs[0]?.run_id ?? null);
      const built = runs.find((entry) => entry.title);
      if (built) api.listDashboards().then((rows) => setKnownSlug(rows.find((row) => row.thread_id === thread)?.slug ?? null)).catch(() => undefined);
    }).catch(() => undefined);
  }, []);

  useEffect(() => {
    if (!threadFromUrl) return;
    setThreadId(threadFromUrl);
    loadHistory(threadFromUrl);
  }, [threadFromUrl, loadHistory]);

  useEffect(() => { if (threadId && status && status !== "running") loadHistory(threadId); }, [status, threadId, loadHistory]);
  useEffect(() => { if (status === "awaiting_approval") setTab("plan"); }, [status]);

  async function send(text: string) {
    setError(null);
    try {
      // The agent can't see inside the dashboard, so it's told what the person is looking at.
      const context = selection.length ? `${CONTEXT_MARK} ${describeSelection(selection)}.)` : "";
      const started = await api.startRun(text + context, threadId, requireApproval);
      setThreadId(started.thread_id);
      setRunId(started.run_id);
      setPrompt("");
      setTab("progress");
      setHistory((current) => [...current, { run_id: started.run_id, thread_id: started.thread_id, prompt: text, status: "running", started_at: new Date().toISOString(), title: null, summary: null }]);
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
      setTab("progress");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The run couldn't be resumed.");
    }
  }

  function startOver() {
    setThreadId(null); setRunId(null); setKnownSlug(null); setSelection([]); setError(null); setHistory([]); setWide(false);
    router.replace("/builder");
  }

  if (!threadId) {
    return (
      <div className="h-full">
        {error && <div className="p-4"><ErrorState message={error} /></div>}
        <Welcome onSend={send} requireApproval={requireApproval} setRequireApproval={setRequireApproval} />
      </div>
    );
  }

  const showBuild = running || status === "awaiting_approval";
  const tabs: [Tab, string][] = [["progress", "Progress"], ["plan", "Plan"], ["timing", "Timing"]];

  return (
    <div className={cn("grid h-full", wide ? "grid-cols-1" : "grid-cols-[372px_minmax(0,1fr)]")}>
      <section className={cn("flex min-h-0 flex-col border-r border-border bg-card", wide && "hidden")} aria-label="Assistant">
        <header className="flex h-14 shrink-0 items-center justify-between border-b border-border px-4">
          <div className="flex gap-1" role="tablist">
            {tabs.map(([id, label]) => (
              <button key={id} role="tab" aria-selected={tab === id} onClick={() => setTab(id)}
                className={cn("rounded-md px-2.5 py-1 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground", tab === id && "bg-muted text-foreground")}>
                {label}
              </button>
            ))}
          </div>
          <Button size="sm" variant="ghost" onClick={startOver}><Plus className="size-3.5" /> New</Button>
        </header>

        <div className="min-h-0 flex-1 overflow-auto p-4">
          {tab === "progress" && (
            <div className="space-y-4">
              {history.map((entry) => (
                <div key={entry.run_id} className="space-y-2">
                  <p className="ml-6 rounded-xl rounded-br-sm bg-accent px-3 py-2 text-[13px] leading-relaxed">{entry.prompt.split(CONTEXT_MARK)[0]}</p>
                  {entry.run_id !== runId && entry.summary && <p className="text-[13px] leading-relaxed text-muted-foreground">{entry.summary}</p>}
                </div>
              ))}

              {error && <ErrorState message={error} />}
              {lost && running && <ErrorState message="Lost the connection to the API. Reconnecting…" />}

              {runId && <ProgressSteps milestones={milestones} />}

              {status === "awaiting_answer" && result?.question && (
                <div className="space-y-2 rounded-lg border border-border p-3">
                  <p className="text-[13px]">{result.question}</p>
                  <textarea ref={answer} aria-label="Your answer" rows={2} className="w-full rounded-lg border border-border bg-background p-2 text-[13px]" />
                  <Button size="sm" onClick={() => resume({ answer: answer.current?.value ?? "" })}>Reply</Button>
                </div>
              )}

              {result?.error && <ErrorState message={result.error} />}
              {result?.summary && (
                <div className="space-y-2 text-[13px] leading-relaxed">
                  <p>{result.summary}</p>
                  {(result.definitions?.length ?? 0) > 0 && (
                    <details>
                      <summary className="cursor-pointer text-xs font-medium text-primary">Definitions used</summary>
                      <ul className="mt-1.5 list-disc space-y-1 pl-4 text-xs text-muted-foreground">
                        {result.definitions?.map((definition) => <li key={definition}>{definition}</li>)}
                      </ul>
                    </details>
                  )}
                  {result.warnings?.map((warning) => <p key={warning} className="text-xs text-warning">{warning}</p>)}
                </div>
              )}

              {stages.length > 0 && (
                <details>
                  <summary className="cursor-pointer text-xs font-medium text-muted-foreground hover:text-foreground">Show every step</summary>
                  <div className="mt-2"><RunTimeline stages={stages} active={running} /></div>
                </details>
              )}
            </div>
          )}

          {tab === "plan" && (plan
            ? <PlanCard plan={plan} editable={status === "awaiting_approval"} onApprove={(edited) => resume({ plan: edited })} onReject={(reason) => resume({ rejected: true, reason })} />
            : <EmptyState title="No plan yet">It appears here in a few seconds.</EmptyState>)}

          {tab === "timing" && (run?.timing && run.timing.turns > 0
            ? <TimingPanel timing={run.timing} wallSeconds={elapsed} />
            : <EmptyState title="Nothing to show yet">Timing appears once the agent starts.</EmptyState>)}
        </div>

        <footer className="shrink-0 border-t border-border p-3">
          {selection.length > 0 && (
            <p className="mb-2 flex items-start gap-1.5 text-[11px] leading-snug text-muted-foreground">
              <Filter className="mt-0.5 size-3 shrink-0" /> The agent will know you&apos;re viewing {describeSelection(selection)}
            </p>
          )}
          <div className="flex items-end gap-2 rounded-xl border border-border bg-background p-2 focus-within:border-primary">
            <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} rows={2} aria-label="Ask for a change"
              placeholder="Ask for a change…" className="min-w-0 flex-1 resize-none bg-transparent px-1 text-[13px] leading-relaxed outline-none placeholder:text-muted-foreground"
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && prompt.trim() && !running) { e.preventDefault(); send(prompt); } }} />
            <Button size="icon" aria-label="Send" disabled={!prompt.trim() || running} onClick={() => send(prompt)}><ArrowUp className="size-4" /></Button>
          </div>
        </footer>
      </section>

      <section className="flex min-h-0 min-w-0 flex-col" aria-label="Dashboard">
        <header className="flex h-14 shrink-0 items-center gap-2.5 border-b border-border bg-card px-4">
          <h1 className="min-w-0 truncate text-sm font-semibold tracking-tight">{title ?? "New dashboard"}</h1>
          <Status status={status} hasDashboard={Boolean(slug)} warnings={result?.warnings?.length ?? 0} />
          {selection.map((filter) => (
            <Badge key={filter.name} variant="primary" data-testid="selected-filter"><Filter className="size-3" /> {filter.name}: {filter.values.join(", ")}</Badge>
          ))}
          <div className="ml-auto flex items-center gap-1">
            <Button size="sm" variant="ghost" onClick={() => setWide(!wide)}>
              {wide ? <Minimize2 className="size-3.5" /> : <Maximize2 className="size-3.5" />} {wide ? "Show assistant" : "Full width"}
            </Button>
            {slug && config && (
              <a className="inline-flex h-8 items-center gap-1.5 rounded-lg px-2.5 text-xs font-medium text-muted-foreground hover:bg-muted hover:text-foreground"
                href={`${config.superset_url}/superset/dashboard/${slug}/`} target="_blank" rel="noreferrer">
                Open in Superset <ExternalLink className="size-3" />
              </a>
            )}
          </div>
        </header>

        <div className="relative min-h-0 flex-1">
          {slug && <DashboardEmbed key={embedKey} slug={slug} onSelection={setSelection} onLoaded={markLoaded} />}
          {(showBuild && !embedLoaded) && (
            <div className="absolute inset-0 z-10 overflow-auto bg-background">
              {plan ? <LiveCanvas plan={plan} events={events} /> : <CanvasPlaceholder />}
            </div>
          )}
          {!slug && !showBuild && (
            <div className="flex h-full items-center justify-center">
              {runId && !run ? <Loading /> : <EmptyState title="No dashboard yet">{result?.error ?? "Ask for one on the left."}</EmptyState>}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
