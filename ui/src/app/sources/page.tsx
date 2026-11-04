"use client";

import { Database } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

import { EmptyState, ErrorState, Loading, PageHeader } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { api, type SourceRow } from "@/lib/api";

export default function SourcesPage() {
  const [rows, setRows] = useState<SourceRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { api.sources().then(setRows).catch((cause) => setError(cause.message)); }, []);

  return (
    <div className="mx-auto max-w-4xl space-y-5 p-8">
      <PageHeader title="Sources" description="The data Nova can read. Every source is connected with read-only credentials." />
      {error && <ErrorState message={error} />}
      {!rows && !error && <Loading />}
      {rows?.length === 0 && <EmptyState title="No sources connected yet">Run <code>make skill</code> to add one.</EmptyState>}
      {rows && rows.length > 0 && (
        <Card className="divide-y divide-border">
          {rows.map((source) => (
            <div key={source.slug} className="flex items-center gap-3 px-4 py-3">
              <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-muted text-muted-foreground"><Database className="size-4" /></span>
              <div className="min-w-0 flex-1">
                <h2 className="text-[13px] font-semibold">{source.display_name}</h2>
                <p className="text-xs text-muted-foreground">{source.dialect} · <span className="font-mono text-[11px]">{source.trino_catalog}</span></p>
              </div>
              {source.skill ? (
                <Link href={`/skills/${source.skill.name}`}>
                  <Badge variant={source.skill.status === "reviewed" ? "success" : "warning"}>Skill v{source.skill.version}, {source.skill.status}</Badge>
                </Link>
              ) : <Badge>No skill yet</Badge>}
            </div>
          ))}
        </Card>
      )}
    </div>
  );
}
