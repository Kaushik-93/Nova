"use client";

import { RefreshCw } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { Dashboard, type FilterSelection } from "@/components/dashboard";
import { EmptyState, ErrorState, Loading } from "@/components/states";
import { Button } from "@/components/ui/button";
import { api, type DashboardDetail } from "@/lib/api";

/** Rebuilds a sheet made in Superset, before Nova drew its own: the same request, in the same project,
 *  in a fresh conversation, since the old one's steps used tools that no longer exist. */
function Rebuild({ detail }: { detail: DashboardDetail }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function rebuild() {
    if (!detail.prompt || !detail.project) return;
    setBusy(true);
    try {
      const started = await api.startRun(detail.prompt, null, false, detail.project.slug);
      await api.deleteSheet(detail.project.slug, detail.slug);
      router.push(`/builder?thread=${started.thread_id}`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "It couldn't be rebuilt.");
      setBusy(false);
    }
  }
  return (
    <div className="mx-auto max-w-lg space-y-3 p-8">
      <EmptyState title="This sheet needs rebuilding">
        It was built before Nova drew its own dashboards on the semantic layer.
        {detail.prompt && <> It was asked for as: <q className="text-foreground">{detail.prompt.split("\n\n(")[0]}</q></>}
      </EmptyState>
      {error && <ErrorState message={error} />}
      {detail.can_edit && detail.prompt && detail.project && (
        <div className="text-center">
          <Button onClick={rebuild} disabled={busy}><RefreshCw className={busy ? "size-3.5 animate-spin" : "size-3.5"} /> Rebuild it</Button>
        </div>
      )}
    </div>
  );
}

export function Sheet({ slug, version = 0, onSelection, onLoaded }: { slug: string; version?: number;
  onSelection?: (selection: FilterSelection) => void; onLoaded?: (detail: DashboardDetail) => void }) {
  const [detail, setDetail] = useState<DashboardDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let current = true;
    api.dashboard(slug).then((found) => { if (current) { setDetail(found); onLoaded?.(found); } })
      .catch((cause) => { if (current) setError(cause.message); });
    return () => { current = false; };
  }, [slug, version]); // eslint-disable-line react-hooks/exhaustive-deps

  if (error) return <div className="p-4"><ErrorState message={error} /></div>;
  if (!detail) return <Loading />;
  if (detail.needs_rebuild || !detail.spec) return <Rebuild detail={detail} />;
  return <Dashboard slug={slug} spec={detail.spec} version={version} onSelection={onSelection} />;
}
