"use client";

import { AlertTriangle, Building2, Database, Loader2, Plus, RotateCw } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { ConnectForm } from "@/app/sources/connect-form";
import { ConnectProgress } from "@/app/sources/connect-progress";
import { TenancyPanel } from "@/app/sources/tenancy-panel";
import { ErrorState, Loading, PageHeader } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { api, type DemoSource, type SourceRow, type Started } from "@/lib/api";

const POLL_MS = 4000;
const WORKING = ["connecting", "profiling"];
type Job = { runId: string; name: string };

export default function SourcesPage() {
  const [rows, setRows] = useState<SourceRow[] | null>(null);
  const [demos, setDemos] = useState<DemoSource[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [tenancy, setTenancy] = useState<string | null>(null);

  const load = useCallback(() => {
    api.sources().then(setRows).catch((cause) => setError(cause.message));
    api.demoSources().then(setDemos).catch(() => undefined);
  }, []);
  useEffect(load, [load]);

  // A page opened while a source is connecting has no job to follow, so it watches the list.
  const working = rows?.some((source) => WORKING.includes(source.status));
  useEffect(() => {
    if (!working) return;
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, [working, load]);

  const started = (result: Started, name: string) => {
    setAdding(false);
    setJobs((current) => [...current, { runId: result.run_id, name }]);
    load();
  };
  const act = (work: () => Promise<unknown>) => {
    setError(null);
    work().catch((cause) => setError(cause.message));
  };
  const waiting = demos.filter((demo) => !demo.connected);

  return (
    <div className="mx-auto max-w-4xl space-y-5 p-8">
      <PageHeader title="Sources" description="The data Nova can read. Nothing here is available to anyone until you give them access under People.">
        <Button variant="outline" size="sm" onClick={() => setAdding(!adding)}><Plus className="size-3.5" /> Connect a source</Button>
      </PageHeader>
      {error && <ErrorState message={error} />}
      {adding && <ConnectForm onStarted={started} />}
      {jobs.map((job) => <ConnectProgress key={job.runId} runId={job.runId} name={job.name} onDone={load} />)}
      {!rows && !error && <Loading />}

      {rows && rows.length > 0 && (
        <Card className="divide-y divide-border">
          {rows.map((source) => (
            <div key={source.slug}>
              <div className="flex items-center gap-3 px-4 py-3">
                <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-muted text-muted-foreground"><Database className="size-4" /></span>
                <div className="min-w-0 flex-1">
                  <h2 className="text-[13px] font-semibold">{source.display_name}</h2>
                  <p className="text-xs text-muted-foreground">{source.dialect} · <span className="font-mono text-[11px]">{source.trino_catalog}</span></p>
                </div>
                {source.read_only === false && <Badge variant="warning"><AlertTriangle className="size-3" /> Can write</Badge>}
                {source.tenant_column && <Badge><Building2 className="size-3" /> By {source.tenant_column}</Badge>}
                {WORKING.includes(source.status) && <Badge><Loader2 className="size-3 animate-spin" /> Connecting</Badge>}
                {source.status === "failed" && (
                  <>
                    <Badge variant="warning">Failed</Badge>
                    <Button variant="outline" size="sm" onClick={() => act(async () => {
                      const job = await api.profileSource(source.slug);
                      started({ ...job, thread_id: "", slug: source.slug, read_only: true, warning: null }, source.display_name);
                    })}><RotateCw className="size-3.5" /> Retry</Button>
                  </>
                )}
                {source.status === "ready" && (source.skill ? (
                  <Link href={`/skills/${source.skill.name}`}>
                    <Badge variant={source.skill.status === "reviewed" ? "success" : "warning"}>Skill v{source.skill.version}, {source.skill.status}</Badge>
                  </Link>
                ) : <Badge>No skill yet</Badge>)}
                {source.status === "ready" && source.skill && (
                  <Button variant="ghost" size="sm" onClick={() => setTenancy(tenancy === source.slug ? null : source.slug)}>Tenants</Button>
                )}
              </div>
              {tenancy === source.slug && <TenancyPanel slug={source.slug} onSaved={() => { setTenancy(null); load(); }} />}
            </div>
          ))}
        </Card>
      )}

      {waiting.length > 0 && (
        <section className="space-y-2">
          <h2 className="text-[13px] font-semibold">Try it with demo data</h2>
          <div className="grid gap-3 md:grid-cols-3">
            {waiting.map((demo) => (
              <Card key={demo.slug} className="flex flex-col gap-2 p-4">
                <h3 className="text-[13px] font-semibold">{demo.name}</h3>
                <p className="flex-1 text-xs leading-relaxed text-muted-foreground">{demo.description}</p>
                <Button variant="outline" size="sm" onClick={() => act(async () => started(await api.connectDemo(demo.slug), demo.name))}>Connect</Button>
              </Card>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
