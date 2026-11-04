"use client";

import { ExternalLink, MessageSquare } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

import { EmptyState, ErrorState, Loading } from "@/components/states";
import { Card, CardContent } from "@/components/ui/card";
import { api, type Config, type DashboardRow } from "@/lib/api";

export default function DashboardsPage() {
  const [rows, setRows] = useState<DashboardRow[] | null>(null);
  const [config, setConfig] = useState<Config | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.listDashboards().then(setRows).catch((cause) => setError(cause.message));
    api.config().then(setConfig).catch(() => undefined);
  }, []);

  return (
    <div className="mx-auto max-w-4xl space-y-4 p-6">
      <h1 className="text-lg font-semibold">Dashboards</h1>
      {error && <ErrorState message={error} />}
      {!rows && !error && <Loading />}
      {rows?.length === 0 && <EmptyState title="No dashboards yet"><Link className="text-primary" href="/builder">Build your first one</Link></EmptyState>}
      {rows?.map((row) => (
        <Card key={row.slug}>
          <CardContent className="space-y-2">
            <h2 className="text-sm font-semibold">{row.title}</h2>
            {row.summary && <p className="line-clamp-2 text-sm text-muted-foreground">{row.summary}</p>}
            <div className="flex gap-4 text-xs">
              <Link className="flex items-center gap-1 text-primary hover:underline" href={`/builder?thread=${row.thread_id}`}>
                <MessageSquare className="size-3" /> Continue with the agent
              </Link>
              {config && (
                <a className="flex items-center gap-1 text-primary hover:underline" target="_blank" rel="noreferrer"
                  href={`${config.superset_url}/superset/dashboard/${row.slug}/`}>
                  <ExternalLink className="size-3" /> Open in Superset
                </a>
              )}
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
