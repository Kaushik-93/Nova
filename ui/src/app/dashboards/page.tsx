"use client";

import { ExternalLink, LayoutDashboard, MessageSquare, Users } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

import { EmptyState, ErrorState, Loading, PageHeader } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { api, type Config, type DashboardRow } from "@/lib/api";
import { useAuth } from "@/lib/auth";

const DATE = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

export default function DashboardsPage() {
  const [rows, setRows] = useState<DashboardRow[] | null>(null);
  const [config, setConfig] = useState<Config | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { user } = useAuth();
  const isAdmin = user.role === "admin";
  const load = () => api.listDashboards().then(setRows).catch((cause) => setError(cause.message));

  useEffect(() => {
    load();
    api.config().then(setConfig).catch(() => undefined);
  }, []);

  const share = (row: DashboardRow) => {
    setError(null);
    api.shareDashboard(row.slug, !row.shared).then(load).catch((cause) => setError(cause.message));
  };

  return (
    <div className="mx-auto max-w-6xl space-y-5 p-8">
      <PageHeader title="Dashboards" description={user.role === "viewer" ? "The dashboards shared with you." : "Everything you can see. Open one, or continue your own in conversation with the agent."} />
      {error && <ErrorState message={error} />}
      {!rows && !error && <Loading />}
      {rows?.length === 0 && <EmptyState title="No dashboards yet">{user.role === "viewer" ? "Dashboards appear here once they're shared with you." : <Link className="font-medium text-primary" href="/builder">Build your first one</Link>}</EmptyState>}
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {rows?.map((row) => (
          <Card key={row.slug} className="flex flex-col p-4 transition-colors hover:border-primary">
            <Link href={`/dashboards/${row.slug}`} className="flex-1">
              <span className="flex size-8 items-center justify-center rounded-lg bg-accent text-primary"><LayoutDashboard className="size-4" /></span>
              <h2 className="mt-3 text-[13px] font-semibold leading-snug">{row.title}</h2>
              {row.summary && <p className="mt-1 line-clamp-3 text-xs leading-relaxed text-muted-foreground">{row.summary}</p>}
            </Link>
            <div className="mt-3 flex items-center gap-3 border-t border-border pt-2.5 text-[11px] text-muted-foreground">
              <span className="flex-1">{DATE.format(new Date(row.updated_at))}</span>
              {(isAdmin || (row.created_by === user.email && user.role !== "viewer")) && (
                <Link className="flex items-center gap-1 hover:text-foreground" href={`/builder?thread=${row.thread_id}`}>
                  <MessageSquare className="size-3" /> Continue
                </Link>
              )}
              {config?.superset_url && (
                <a className="flex items-center gap-1 hover:text-foreground" target="_blank" rel="noreferrer" href={`${config.superset_url}/superset/dashboard/${row.slug}/`}>
                  Superset <ExternalLink className="size-3" />
                </a>
              )}
              {isAdmin && (
                <Button variant="ghost" size="sm" className="h-6 px-1.5 text-[11px]" aria-pressed={row.shared} onClick={() => share(row)}>
                  <Users className="size-3" /> {row.shared ? "Shared" : "Share"}
                </Button>
              )}
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}
