"use client";

import { BookOpen, ChevronRight } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

import { EmptyState, ErrorState, Loading, PageHeader } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { api, type SourceRow } from "@/lib/api";

export default function SkillsPage() {
  const [rows, setRows] = useState<SourceRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { api.sources().then(setRows).catch((cause) => setError(cause.message)); }, []);
  const skills = rows?.filter((source) => source.skill);

  return (
    <div className="mx-auto max-w-4xl space-y-5 p-8">
      <PageHeader title="Skills" description="What the agent has learned about each source. Review one to make its definitions official." />
      {error && <ErrorState message={error} />}
      {!rows && !error && <Loading />}
      {skills?.length === 0 && <EmptyState title="No skills yet" />}
      {skills && skills.length > 0 && (
        <Card className="divide-y divide-border">
          {skills.map((source) => (
            <Link key={source.slug} href={`/skills/${source.skill!.name}`} className="flex items-center gap-3 px-4 py-3 transition-colors first:rounded-t-xl last:rounded-b-xl hover:bg-muted">
              <span className="flex size-8 items-center justify-center rounded-lg bg-accent text-primary"><BookOpen className="size-4" /></span>
              <span className="min-w-0 flex-1 text-[13px] font-semibold">{source.display_name}</span>
              <Badge variant={source.skill!.status === "reviewed" ? "success" : "warning"}>v{source.skill!.version}, {source.skill!.status}</Badge>
              <ChevronRight className="size-4 text-muted-foreground" />
            </Link>
          ))}
        </Card>
      )}
    </div>
  );
}
