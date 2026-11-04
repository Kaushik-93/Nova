"use client";

import { use, useEffect, useState } from "react";

import { ErrorState, Loading } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api, type Skill } from "@/lib/api";
import { formatNumber } from "@/lib/utils";

export default function SkillPage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = use(params);
  const [skill, setSkill] = useState<Skill | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = () => api.skill(slug).then(setSkill).catch((cause) => setError(cause.message));
  useEffect(() => { load(); }, [slug]); // eslint-disable-line react-hooks/exhaustive-deps

  if (error) return <div className="p-6"><ErrorState message={error} /></div>;
  if (!skill) return <Loading />;

  return (
    <div className="mx-auto max-w-5xl space-y-4 p-6">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-semibold">{skill.name}</h1>
        <Badge variant={skill.status === "reviewed" ? "success" : "warning"}>v{skill.version}, {skill.status}</Badge>
        {skill.status === "draft" && (
          <Button size="sm" className="ml-auto" onClick={() => api.approveSkill(slug).then(load).catch((cause) => setError(cause.message))}>
            Approve these definitions
          </Button>
        )}
      </div>
      <p className="text-sm text-muted-foreground">{skill.description}</p>

      <Card>
        <CardHeader><CardTitle>Metrics</CardTitle></CardHeader>
        <CardContent className="divide-y divide-border p-0">
          {Object.entries(skill.metrics).map(([key, metric]) => (
            <details key={key} className="px-4 py-2">
              <summary className="flex cursor-pointer items-center gap-2 text-sm">
                {metric.label}
                {metric.unit && <span className="text-xs text-muted-foreground">{metric.unit}</span>}
                <Badge className="ml-auto" variant={metric.status === "reviewed" ? "success" : "default"}>{metric.status}</Badge>
              </summary>
              {metric.description && <p className="mt-2 text-xs text-muted-foreground">{metric.description}</p>}
              <pre className="mt-2 overflow-auto rounded bg-muted p-2 text-[11px]">{`${metric.sql}\nFROM ${metric.base}${metric.filters.length ? `\nWHERE ${metric.filters.join("\n  AND ")}` : ""}`}</pre>
            </details>
          ))}
        </CardContent>
      </Card>

      {Object.keys(skill.proposed).length > 0 && (
        <Card>
          <CardHeader><CardTitle>Proposed changes</CardTitle></CardHeader>
          <CardContent className="space-y-2 text-sm">
            {Object.entries(skill.proposed).map(([key, metric]) => <p key={key}>{metric.label}: <code className="text-xs">{metric.sql}</code></p>)}
          </CardContent>
        </Card>
      )}

      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader><CardTitle>Traps it found</CardTitle></CardHeader>
          <CardContent>
            <ul className="space-y-2 text-xs">
              {skill.gotchas.map((gotcha, index) => (
                <li key={index}><span className="font-mono text-muted-foreground">{gotcha.table}.{gotcha.column}</span><br />{gotcha.detail}</li>
              ))}
            </ul>
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>Tables</CardTitle></CardHeader>
          <CardContent>
            <ul className="space-y-1 text-xs">
              {skill.tables.map((table) => (
                <li key={table.name} className="flex justify-between"><span className="font-mono">{table.name}</span><span className="tabular-nums text-muted-foreground">{formatNumber(table.rows ?? 0)}</span></li>
              ))}
            </ul>
          </CardContent>
        </Card>
      </div>

      <Card><CardHeader><CardTitle>Guide</CardTitle></CardHeader><CardContent><p className="whitespace-pre-wrap text-sm leading-relaxed">{skill.guide}</p></CardContent></Card>
      <Card><CardHeader><CardTitle>Tested examples</CardTitle></CardHeader><CardContent><pre className="overflow-auto text-[11px]">{skill.examples}</pre></CardContent></Card>
      <Card><CardHeader><CardTitle>Changelog</CardTitle></CardHeader><CardContent><pre className="whitespace-pre-wrap text-xs">{skill.changelog}</pre></CardContent></Card>
    </div>
  );
}
