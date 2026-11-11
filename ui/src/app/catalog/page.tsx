"use client";

import { ArrowRight, Box, Library } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

import { Page } from "@/components/page";
import { EmptyState, ErrorState, Loading } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { api, type SemanticSource, type SourceRow } from "@/lib/api";
import { useAuth } from "@/lib/auth";

function counts(source: SemanticSource) {
  const members = source.cubes.flatMap((cube) => [...cube.measures, ...cube.dimensions]);
  return {
    cubes: source.cubes.length,
    measures: source.cubes.reduce((sum, cube) => sum + cube.measures.length, 0),
    dimensions: source.cubes.reduce((sum, cube) => sum + cube.dimensions.length, 0),
    reviewed: members.filter((member) => member.status === "reviewed").length,
    total: members.length,
  };
}

function SourceCard({ source, skill }: { source: SemanticSource; skill?: SourceRow["skill"] }) {
  const c = counts(source);
  const share = c.total ? Math.round((c.reviewed / c.total) * 100) : 0;
  return (
    <Link href={`/catalog/${source.source}`} className="group block focus-visible:outline-none">
      <Card className="h-full p-4 transition-colors group-hover:border-primary/40 group-focus-visible:border-primary">
        <div className="flex items-start gap-3">
          <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-accent text-primary"><Box className="size-4" /></span>
          <div className="min-w-0 flex-1">
            <p className="truncate text-[14px] font-semibold">{source.display_name}</p>
            <p className="truncate font-mono text-[11px] text-muted-foreground">{source.catalog}</p>
          </div>
          {skill && <Badge variant={skill.status === "reviewed" ? "success" : "warning"}>v{skill.version} {skill.status}</Badge>}
        </div>
        <dl className="mt-4 grid grid-cols-3 gap-2 text-center">
          {([["Cubes", c.cubes], ["Measures", c.measures], ["Dimensions", c.dimensions]] as const).map(([label, value]) => (
            <div key={label} className="rounded-lg bg-muted px-2 py-2">
              <dd className="text-[15px] font-semibold tabular-nums">{value}</dd>
              <dt className="text-[11px] text-muted-foreground">{label}</dt>
            </div>
          ))}
        </dl>
        <div className="mt-4">
          <div className="flex items-center justify-between text-xs">
            <span className="text-muted-foreground">Reviewed by a person</span>
            <span className="font-medium tabular-nums">{c.reviewed} of {c.total}</span>
          </div>
          <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-muted" role="progressbar" aria-valuenow={share} aria-valuemin={0} aria-valuemax={100}
            aria-label={`${share}% reviewed`}>
            <div className="h-full rounded-full bg-success" style={{ width: `${share}%` }} />
          </div>
        </div>
        <p className="mt-4 inline-flex items-center gap-1 text-xs font-medium text-primary">Browse definitions <ArrowRight className="size-3.5" /></p>
      </Card>
    </Link>
  );
}

export default function CatalogPage() {
  const { user } = useAuth();
  const admin = user.role === "admin";
  const [sources, setSources] = useState<SemanticSource[] | null>(null);
  const [rows, setRows] = useState<SourceRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.semanticMeta().then(setSources).catch((cause) => setError(cause.message));
    api.sources().then(setRows).catch(() => undefined);
  }, []);

  const modelled = new Set(sources?.map((source) => source.source));
  const missing = admin ? rows.filter((row) => row.status === "ready" && !modelled.has(row.slug)) : [];

  return (
    <Page title="Catalog" icon={<Library className="size-4" />}
      description="The definitions every number in Nova comes from: each source's cubes, measures and dimensions.">
      {error && <ErrorState message={error} />}
      {!sources && !error && <Loading />}
      {sources?.length === 0 && missing.length === 0 && (
        <EmptyState title="Nothing to browse yet">
          {admin ? "Connect a source on the Sources page, and Nova will learn it." : "Ask an administrator for access to a source."}
        </EmptyState>
      )}
      {sources && sources.length > 0 && (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {sources.map((source) => <SourceCard key={source.source} source={source} skill={rows.find((row) => row.slug === source.source)?.skill} />)}
        </div>
      )}
      {missing.length > 0 && (
        <Card className="p-4 text-[13px]">
          <p className="font-medium">Not modelled yet</p>
          <p className="mt-1 text-muted-foreground">
            {missing.map((row) => row.display_name).join(", ")} {missing.length === 1 ? "has" : "have"} no semantic model, so nothing can be
            asked of {missing.length === 1 ? "it" : "them"}. Profile {missing.length === 1 ? "it" : "them"} again from <Link href="/sources" className="text-primary underline underline-offset-2">Sources</Link>.
          </p>
        </Card>
      )}
    </Page>
  );
}
