"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { EmptyState, ErrorState, Loading } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { api, type SourceRow } from "@/lib/api";

function useSources() {
  const [rows, setRows] = useState<SourceRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { api.sources().then(setRows).catch((cause) => setError(cause.message)); }, []);
  return { rows, error };
}

export default function SourcesPage() {
  const { rows, error } = useSources();
  return (
    <div className="mx-auto max-w-4xl space-y-4 p-6">
      <h1 className="text-lg font-semibold">Sources</h1>
      {error && <ErrorState message={error} />}
      {!rows && !error && <Loading />}
      {rows?.length === 0 && <EmptyState title="No sources connected yet" />}
      {rows?.map((source) => (
        <Card key={source.slug}>
          <CardContent className="flex items-center justify-between gap-4">
            <div>
              <h2 className="text-sm font-semibold">{source.display_name}</h2>
              <p className="text-xs text-muted-foreground">{source.dialect} · Trino catalog {source.trino_catalog}</p>
            </div>
            {source.skill ? (
              <Link href={`/skills/${source.skill.name}`}>
                <Badge variant={source.skill.status === "reviewed" ? "success" : "warning"}>
                  Skill v{source.skill.version}, {source.skill.status}
                </Badge>
              </Link>
            ) : <Badge>No skill yet</Badge>}
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
