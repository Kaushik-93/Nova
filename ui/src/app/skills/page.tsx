"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { EmptyState, ErrorState, Loading } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { api, type SourceRow } from "@/lib/api";

export default function SkillsPage() {
  const [rows, setRows] = useState<SourceRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { api.sources().then(setRows).catch((cause) => setError(cause.message)); }, []);
  const skills = rows?.filter((source) => source.skill);

  return (
    <div className="mx-auto max-w-4xl space-y-4 p-6">
      <h1 className="text-lg font-semibold">Skills</h1>
      <p className="text-sm text-muted-foreground">What the agent has learned about each source. Review a skill to make its definitions official.</p>
      {error && <ErrorState message={error} />}
      {!rows && !error && <Loading />}
      {skills?.length === 0 && <EmptyState title="No skills yet" />}
      {skills?.map((source) => (
        <Link key={source.slug} href={`/skills/${source.skill!.name}`} className="block">
          <Card className="hover:bg-muted">
            <CardContent className="flex items-center justify-between">
              <span className="text-sm font-semibold">{source.display_name}</span>
              <Badge variant={source.skill!.status === "reviewed" ? "success" : "warning"}>v{source.skill!.version}, {source.skill!.status}</Badge>
            </CardContent>
          </Card>
        </Link>
      ))}
    </div>
  );
}
